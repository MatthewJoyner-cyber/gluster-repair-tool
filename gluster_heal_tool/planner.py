# SPDX-License-Identifier: GPL-2.0-only
"""Repair planning helpers."""
from __future__ import annotations

from collections import Counter
from posixpath import dirname

from .controller_paths import default_stage_local_path
from .directory_tie import bounded_tree_diff
from .models import ManifestObject, PlanAction, ResolutionObservation
from .role_safety import role_for_host
from .execution_plan import annotate_execution_waves
from .planner_graph import annotate_plan
from .planner_report import render_plan_summary, summarize_plan, write_plan
from .type_mismatch import type_mismatch_quarantine_recommendation


_SOFT_DIRECTORY_CHILD_DEPENDENCY_ACTION_TYPES = {
    "review_file_metadata",
    "review_directory_metadata",
    "repair_directory_metadata",
    "review_probable_stale_survivor",
    "review_probable_orphaned_symlink",
}


def _append_graph_marker(action: PlanAction, marker: str) -> None:
    if marker and marker not in action.graph_markers:
        action.graph_markers.append(marker)


def _attach_role_evidence(action: PlanAction, obj: ManifestObject) -> None:
    action.brick_roles_by_host = dict(obj.brick_roles_by_host)
    action.brick_host_aliases = {
        host: list(aliases)
        for host, aliases in obj.brick_host_aliases.items()
    }
    action.brick_role_evidence_required = bool(obj.brick_role_evidence_required)
    action.brick_role_evidence_error = str(obj.brick_role_evidence_error or "")
    if action.brick_role_evidence_required and action.brick_role_evidence_error:
        action.notes.append("brick role evidence error: " + action.brick_role_evidence_error)
    if "arbiter" in action.brick_roles_by_host.values():
        action.notes.append("arbiter evidence is metadata-only support; arbiter bricks are not payload sources")


def _is_arbiter_host(obj: ManifestObject, host: str) -> bool:
    return role_for_host(obj.brick_roles_by_host, host, obj.brick_host_aliases) == "arbiter"


def _role_for_observation(obj: ManifestObject, host: str) -> str:
    return role_for_host(obj.brick_roles_by_host, host, obj.brick_host_aliases)


def _directory_child_dependency_is_soft(dep: PlanAction | None) -> bool:
    if dep is None:
        return False
    if dep.action_type in _SOFT_DIRECTORY_CHILD_DEPENDENCY_ACTION_TYPES:
        return True
    return (
        dep.action_type == "review_entry_split_brain"
        and dep.repair_strategy == "replace_entry_split_brain_file"
    )


def _normalize_split_brain_policy(policy: str) -> str:
    normalized = str(policy or "").strip().lower()
    if normalized == "replace":
        normalized = "auto"
    if normalized in {"off", "review", "skip"}:
        return "review"
    if normalized in {"auto", "majority", "mtime", "ctime", "size"}:
        return normalized
    return "review"


def _directory_child_dependency_is_promotable(
    dep: PlanAction | None,
    *,
    split_brain_policy: str = "auto",
) -> bool:
    if dep is None:
        return False
    if dep.action_type in {"repair_file", "reconcile_directory"}:
        return True
    if dep.action_type == "review_probable_stale_survivor" and str(dep.repair_strategy or "").startswith(
        "delete_below_quorum"
    ):
        return True
    if dep.action_type == "review_probable_orphaned_symlink" and str(dep.repair_strategy or "").startswith(
        "delete_orphaned_symlink"
    ):
        return True
    if dep.action_type == "review_entry_split_brain":
        if dep.repair_strategy == "replace_entry_split_brain_file":
            return True
        if dep.repair_strategy == "ambiguous_entry_split_brain_file":
            return _normalize_split_brain_policy(split_brain_policy) in {"auto", "mtime", "size"}
    return False


def _verification_notes(action_type: str, repair_strategy: str) -> list[str]:
    if repair_strategy == "recreate_missing_directory_backend":
        return ["post-repair verification: use a temp mount and stat the recreated directory"]
    if action_type in {"repair_file", "repair_file_metadata", "repair_posix_metadata", "reconcile_directory", "repair_directory_metadata"}:
        return ["post-repair verification: use a temp mount and stat the repaired logical path"]
    if action_type == "review_entry_split_brain":
        return [
            "post-repair verification: use a temp mount and stat the repaired logical path after the split-brain resolver or fallback runs"
        ]
    if action_type in {"cleanup_dead_gfid", "cleanup_dead_file_refs"}:
        return [
            "optional verification: use an aux-gfid-mount when you need GFID-centric confirmation of residue cleanup"
        ]
    if action_type == "cleanup_orphaned_symlink":
        return [
            "post-repair verification: use a temp mount if you want to confirm the logical path remains absent after backend cleanup"
        ]
    return []


def _symlink_terminal_object(obs: ResolutionObservation) -> tuple[str, str]:
    candidates = [
        (obs.backend_terminal_lstat_type, obs.backend_terminal_path),
        (obs.gfid_path_terminal_lstat_type, obs.gfid_path_terminal_path),
    ]
    for terminal_kind, terminal_path in candidates:
        if terminal_kind and terminal_path and "/.glusterfs/" not in terminal_path and not terminal_path.endswith("/.glusterfs"):
            return terminal_kind, terminal_path
    return "", ""


def _file_copy_identity(obs: ResolutionObservation) -> str:
    if obs.backend_trusted_gfid:
        return obs.backend_trusted_gfid
    if obs.backend_lstat_type == "symlink":
        _, terminal_path = _symlink_terminal_object(obs)
        if terminal_path:
            terminal_identity = obs.backend_terminal_trusted_gfid or obs.gfid_path_terminal_trusted_gfid
            if terminal_identity:
                return terminal_identity
        if obs.gfid:
            return obs.gfid
    if obs.file_gfid:
        return obs.file_gfid
    if obs.gfid:
        return obs.gfid
    if obs.gfid_path_terminal_trusted_gfid:
        return obs.gfid_path_terminal_trusted_gfid
    return ""


def _file_observation_is_present(obs: ResolutionObservation) -> bool:
    if obs.backend_exists and obs.backend_lstat_type == "file":
        return True
    if obs.backend_lexists and obs.backend_lstat_type == "symlink":
        return True
    return False


def _file_metadata_mismatch_hosts(obj: ManifestObject, canonical_gfid: str) -> list[str]:
    mismatch_hosts: list[str] = []
    if not canonical_gfid:
        return mismatch_hosts
    for host, observations in sorted(obj.observations.items()):
        if _is_arbiter_host(obj, host):
            continue
        for obs in observations:
            if not _file_observation_is_present(obs):
                continue
            if obs.backend_trusted_gfid != canonical_gfid:
                mismatch_hosts.append(host)
                break
    return mismatch_hosts


def _posix_metadata_record(obs: ResolutionObservation) -> dict[str, object]:
    return {
        "mode_bits": int(obs.backend_mode_bits) if obs.backend_mode_bits is not None else None,
        "uid": int(obs.backend_uid) if obs.backend_uid is not None else None,
        "gid": int(obs.backend_gid) if obs.backend_gid is not None else None,
        "acl_access": str(obs.backend_acl_access_text or ""),
        "acl_default": str(obs.backend_acl_default_text or ""),
    }


def _posix_metadata_signature(record: dict[str, object]) -> tuple[object, ...]:
    return (
        record.get("mode_bits"),
        record.get("uid"),
        record.get("gid"),
        record.get("acl_access", ""),
        record.get("acl_default", ""),
    )


def _best_file_copy(obj: ManifestObject) -> ResolutionObservation | None:
    candidates: list[ResolutionObservation] = []
    for host, observations in obj.observations.items():
        if _is_arbiter_host(obj, host):
            continue
        for obs in observations:
            if _file_observation_is_present(obs):
                candidates.append(obs)
    if not candidates:
        return None
    identity_counts: dict[str, int] = {}
    for item in candidates:
        identity = _file_copy_identity(item)
        if identity:
            identity_counts[identity] = identity_counts.get(identity, 0) + 1
    return sorted(
        candidates,
        key=lambda item: (
            identity_counts.get(_file_copy_identity(item), 0),
            item.backend_mtime or -1,
            item.host,
        ),
        reverse=True,
    )[0]


def _directory_hosts(obj: ManifestObject) -> list[str]:
    return sorted(obj.observations.keys())


def _valid_backend_reference(obs: ResolutionObservation) -> bool:
    return bool(obs.backend and obs.relpath)


def _file_target_backend_by_host(obj: ManifestObject) -> dict[str, str]:
    """Return only unambiguous, logical-path-anchored file targets per host."""
    candidates: dict[str, set[str]] = {}
    for host, observations in obj.observations.items():
        for obs in observations:
            if not _valid_backend_reference(obs) or not _backend_root_for_observation(obs):
                continue
            candidates.setdefault(host, set()).add(obs.backend)
    return {
        host: next(iter(paths))
        for host, paths in candidates.items()
        if len(paths) == 1
    }


def _directory_present(obs: ResolutionObservation) -> bool:
    return obs.backend_exists and obs.backend_lstat_type == "dir"


def _directory_has_gfid_conflict(obj: ManifestObject) -> bool:
    if "dir_name_gfid_conflict" in obj.notes:
        return True
    directory_gfids = sorted(
        {
            obs.backend_trusted_gfid or obs.gfid
            for observations in obj.observations.values()
            for obs in observations
            if obs.backend_lstat_type == "dir" and (obs.backend_trusted_gfid or obs.gfid)
        }
    )
    return len(directory_gfids) > 1 or (
        len(obj.gfids) > 1 and ("saw_dir" in obj.notes or obj.object_type in {"directory", "directory_candidate"})
    )


def _mount_access_errors(obj: ManifestObject) -> list[str]:
    relevant = [
        obs
        for observations in obj.observations.values()
        for obs in observations
        if obs.mounted_checked
    ]
    errors: list[str] = []
    for obs in relevant:
        error = str(obs.mounted_error or "").strip()
        if error and error not in errors:
            errors.append(error)
    return errors


def _mount_access_error_notes(obj: ManifestObject) -> list[str]:
    errors = _mount_access_errors(obj)
    if not errors:
        return []
    notes = [f"mount access errors observed: {'; '.join(errors[:3])}"]
    lowered = " ".join(errors).lower()
    if (
        "input/output" in lowered
        or "eio" in lowered
        or "transport endpoint is not connected" in lowered
        or "enotconn" in lowered
    ):
        notes.append(
            "mount returned EIO/ENOTCONN; treat this as split-brain-like or below-quorum survivor evidence even if Gluster split-brain info reports zero"
        )
    return notes


def _mount_access_problem(obj: ManifestObject) -> bool:
    relevant = [
        obs
        for observations in obj.observations.values()
        for obs in observations
        if obs.mounted_checked
    ]
    if not relevant:
        return False
    if _mount_access_errors(obj):
        return True
    return any(obs.backend_exists for obs in relevant) and not any(obs.mounted_exists for obs in relevant)


def _mounted_accessible(obj: ManifestObject) -> bool:
    return any(
        obs.mounted_checked and obs.mounted_exists
        for observations in obj.observations.values()
        for obs in observations
    )


def _replica_count(obj: ManifestObject) -> int:
    return max(len(obj.observations), 1)


def _data_replica_count(obj: ManifestObject) -> int:
    data_hosts = {host for host in obj.observations if not _is_arbiter_host(obj, host)}
    return max(len(data_hosts), 1) if data_hosts else _replica_count(obj)


def _effective_content_quorum_threshold(obj: ManifestObject, configured_quorum: int) -> int:
    return max(1, min(configured_quorum, _data_replica_count(obj)))


def _majority_threshold(obj: ManifestObject) -> int:
    return (_replica_count(obj) // 2) + 1


def _effective_quorum_threshold(obj: ManifestObject, configured_quorum: int) -> int:
    return max(1, min(configured_quorum, _replica_count(obj)))


def _file_present_hosts(obj: ManifestObject) -> list[str]:
    return sorted(
        host
        for host, observations in obj.observations.items()
        if not _is_arbiter_host(obj, host)
        and any(_file_observation_is_present(obs) for obs in observations)
    )


def _backend_root_for_observation(obs: ResolutionObservation) -> str:
    backend = str(obs.backend or "")
    relpath = str(obs.relpath or "").strip("/")
    suffix = f"/{relpath}" if relpath else ""
    if backend and suffix and backend.endswith(suffix):
        return backend[: -len(suffix)]
    return ""


def _arbiter_missing_gfid_hosts(
    obj: ManifestObject,
    canonical_gfid: str,
    *,
    object_type: str,
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Return arbiter placeholders safe to repair after a checked GFID fault.

    Two matching data bricks are authoritative for the arbiter's metadata-only
    identity. The arbiter may therefore be missing that GFID, have a conflicting
    GFID, or lack the matching local .glusterfs handle.
    """
    if not canonical_gfid:
        return [], {}, {}
    data_hosts = {
        host for host in obj.observations if _role_for_observation(obj, host) == "data"
    }
    arbiter_hosts = {
        host for host in obj.observations if _is_arbiter_host(obj, host)
    }
    if len(data_hosts) != 2 or len(arbiter_hosts) != 1:
        return [], {}, {}
    if object_type == "directory":
        present = _directory_present
    else:
        present = _file_observation_is_present
    if not all(
        any(present(obs) and obs.backend_trusted_gfid == canonical_gfid for obs in obj.observations[host])
        for host in data_hosts
    ):
        return [], {}, {}
    hosts: list[str] = []
    roots: dict[str, str] = {}
    stale_paths: dict[str, str] = {}
    for host in sorted(arbiter_hosts):
        target = next(
            (
                obs
                for obs in obj.observations[host]
                if (
                    present(obs)
                    and _backend_root_for_observation(obs)
                    and (
                        not obs.backend_trusted_gfid
                        or obs.backend_trusted_gfid != canonical_gfid
                        or (
                            obs.backend_trusted_gfid == canonical_gfid
                            and obs.backend_gfid_path_checked
                            and not obs.backend_gfid_path_lexists
                        )
                    )
                )
            ),
            None,
        )
        if target is not None:
            hosts.append(host)
            roots[host] = _backend_root_for_observation(target)
            if (
                target.backend_trusted_gfid
                and target.backend_trusted_gfid != canonical_gfid
                and target.backend_gfid_path_checked
                and target.backend_gfid_path_lexists
                and target.backend_gfid_path
            ):
                stale_paths[host] = target.backend_gfid_path
    return hosts, roots, stale_paths


def _file_identity_and_handles_are_verified(
    obj: ManifestObject,
    canonical_gfid: str,
) -> bool:
    """Return true only when every backend reports the same checked identity handle."""
    if not canonical_gfid:
        return False
    for observations in obj.observations.values():
        target = next((obs for obs in observations if _file_observation_is_present(obs)), None)
        if target is None:
            return False
        if target.backend_trusted_gfid != canonical_gfid:
            return False
        if not target.backend_gfid_path_checked or not target.backend_gfid_path_lexists:
            return False
    return True


def _directory_identity_and_handles_are_verified(
    obj: ManifestObject,
    canonical_gfid: str,
) -> bool:
    """Return true only when every backend reports the same checked directory handle."""
    if not canonical_gfid:
        return False
    for observations in obj.observations.values():
        target = next((obs for obs in observations if _directory_present(obs)), None)
        if target is None:
            return False
        if target.backend_trusted_gfid != canonical_gfid:
            return False
        if not target.backend_gfid_path_checked or not target.backend_gfid_path_lexists:
            return False
    return True


def _arbiter_only_residue_hosts(
    obj: ManifestObject,
    *,
    object_type: str,
) -> list[str]:
    """Recognize a replica-3 arbiter placeholder after both data copies were deleted."""
    data_hosts = {
        host for host in obj.observations if _role_for_observation(obj, host) == "data"
    }
    arbiter_hosts = {
        host for host in obj.observations if _is_arbiter_host(obj, host)
    }
    if len(data_hosts) != 2 or len(arbiter_hosts) != 1:
        return []
    present = _directory_present if object_type == "directory" else _file_observation_is_present
    if any(any(present(obs) for obs in obj.observations[host]) for host in data_hosts):
        return []
    return sorted(
        host
        for host in arbiter_hosts
        if any(present(obs) for obs in obj.observations[host])
    )


def _arbiter_supports_single_data_file_restore(
    obj: ManifestObject,
    winner: ResolutionObservation | None,
    *,
    present_hosts: list[str],
    missing_hosts: list[str],
) -> bool:
    if winner is None:
        return False
    data_hosts = {
        host for host in obj.observations if _role_for_observation(obj, host) == "data"
    }
    arbiter_hosts = {
        host for host in obj.observations if _is_arbiter_host(obj, host)
    }
    if len(data_hosts) != 2 or not arbiter_hosts:
        return False
    if set(present_hosts) | set(missing_hosts) != data_hosts or len(present_hosts) != 1:
        return False
    return any(
        _matches_winner(obs, winner)
        for host in arbiter_hosts
        for obs in obj.observations.get(host, [])
    )


def _arbiter_supports_data_file_winner(
    obj: ManifestObject,
    winner: ResolutionObservation | None,
    *,
    conflict_hosts: list[str],
) -> bool:
    """A matching arbiter identity can corroborate one data-brick payload winner."""
    if winner is None or _is_arbiter_host(obj, winner.host):
        return False
    data_hosts = {
        host for host in obj.observations if _role_for_observation(obj, host) == "data"
    }
    arbiter_hosts = {host for host in obj.observations if _is_arbiter_host(obj, host)}
    if len(data_hosts) != 2 or len(arbiter_hosts) != 1:
        return False
    if set(conflict_hosts) != data_hosts - {winner.host}:
        return False
    return any(
        _matches_winner(obs, winner)
        for host in arbiter_hosts
        for obs in obj.observations.get(host, [])
    )


def _arbiter_backed_data_file_winner(obj: ManifestObject) -> ResolutionObservation | None:
    """Return the sole data copy whose identity matches the arbiter placeholder."""
    data_hosts = sorted(
        host for host in obj.observations if _role_for_observation(obj, host) == "data"
    )
    arbiter_hosts = sorted(host for host in obj.observations if _is_arbiter_host(obj, host))
    if len(data_hosts) != 2 or len(arbiter_hosts) != 1:
        return None

    arbiter_observations = obj.observations.get(arbiter_hosts[0], [])
    matched: list[ResolutionObservation] = []
    for host in data_hosts:
        for observation in obj.observations.get(host, []):
            if not _file_observation_is_present(observation):
                continue
            if any(_matches_winner(arbiter_observation, observation) for arbiter_observation in arbiter_observations):
                matched.append(observation)
    if len({observation.host for observation in matched}) != 1:
        return None
    matching_host = matched[0].host
    other_host = next(host for host in data_hosts if host != matching_host)
    if not any(_file_observation_is_present(observation) for observation in obj.observations.get(other_host, [])):
        return None
    return matched[0]


def _directory_present_hosts(obj: ManifestObject) -> list[str]:
    return sorted(
        host
        for host, observations in obj.observations.items()
        if not _is_arbiter_host(obj, host)
        and any(_directory_present(obs) for obs in observations)
    )


def _file_copy_records(
    obj: ManifestObject,
    *,
    include_arbiters: bool = False,
) -> list[dict[str, object]]:
    copies: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for host, observations in sorted(obj.observations.items()):
        if not include_arbiters and _is_arbiter_host(obj, host):
            continue
        for obs in observations:
            if not _file_observation_is_present(obs):
                continue
            identity = _file_copy_identity(obs)
            key = (host, obs.backend, identity)
            if key in seen:
                continue
            seen.add(key)
            copies.append(
                {
                    "host": host,
                    "backend": obs.backend,
                    "identity": identity,
                    "backend_trusted_gfid": obs.backend_trusted_gfid,
                    "file_gfid": obs.file_gfid,
                    "file_gfid_path": obs.file_gfid_path,
                    "gfid_path": obs.gfid_path,
                    "size": obs.backend_size,
                    "mtime": obs.backend_mtime,
                }
            )
    return copies


def _file_cohort_records(obj: ManifestObject) -> list[dict[str, object]]:
    cohorts: dict[str, dict[str, object]] = {}
    for item in _file_copy_records(obj):
        identity = str(item.get("identity") or f"host:{item['host']}:{item['backend']}")
        cohort = cohorts.setdefault(
            identity,
            {
                "identity": identity,
                "hosts": [],
                "backends": [],
                "latest_mtime": None,
                "largest_size": None,
            },
        )
        host = str(item["host"])
        backend = str(item["backend"])
        if host not in cohort["hosts"]:
            cohort["hosts"].append(host)
        if backend not in cohort["backends"]:
            cohort["backends"].append(backend)
        mtime = item.get("mtime")
        if isinstance(mtime, int):
            latest = cohort.get("latest_mtime")
            cohort["latest_mtime"] = mtime if latest is None else max(int(latest), mtime)
        size = item.get("size")
        if isinstance(size, int):
            largest = cohort.get("largest_size")
            cohort["largest_size"] = size if largest is None else max(int(largest), size)
    return sorted(
        cohorts.values(),
        key=lambda item: (
            len(item["hosts"]),
            item["latest_mtime"] if isinstance(item["latest_mtime"], int) else -1,
            item["largest_size"] if isinstance(item["largest_size"], int) else -1,
            item["identity"],
        ),
        reverse=True,
    )


def _directory_gfid_path_for_backend(backend: str, logical_path: str, gfid: str) -> str:
    clean_gfid = str(gfid or "").strip().lower().replace("-", "")
    suffix = "/" + str(logical_path or "").strip("/")
    if len(clean_gfid) != 32 or not suffix or not backend.endswith(suffix):
        return ""
    backend_root = backend[: -len(suffix)].rstrip("/")
    if not backend_root:
        return ""
    return f"{backend_root}/.glusterfs/{clean_gfid[:2]}/{clean_gfid[2:4]}/{gfid}"


def _directory_copy_records(
    obj: ManifestObject,
    logical_path: str = "",
    *,
    include_arbiters: bool = False,
) -> list[dict[str, object]]:
    copies: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for host, observations in sorted(obj.observations.items()):
        if not include_arbiters and _is_arbiter_host(obj, host):
            continue
        for obs in observations:
            if not (obs.backend_exists and obs.backend_lstat_type == "dir"):
                continue
            identity = obs.backend_trusted_gfid or obs.gfid or ""
            key = (host, obs.backend, identity)
            if key in seen:
                continue
            seen.add(key)
            copies.append(
                {
                    "host": host,
                    "backend": obs.backend,
                    "identity": identity,
                    "backend_trusted_gfid": obs.backend_trusted_gfid,
                    "gfid": obs.gfid,
                    "gfid_path": obs.gfid_path or _directory_gfid_path_for_backend(
                        obs.backend,
                        logical_path,
                        obs.backend_trusted_gfid or obs.gfid,
                    ),
                    "size": obs.backend_size,
                    "mtime": obs.backend_mtime,
                }
            )
    return copies


def _directory_canonical_copy(obj: ManifestObject) -> dict[str, object] | None:
    copies = _directory_copy_records(obj)
    if not copies:
        return None
    identity_counts: dict[str, int] = {}
    for item in copies:
        identity = str(item.get("identity") or "")
        if identity:
            identity_counts[identity] = identity_counts.get(identity, 0) + 1
    return sorted(
        copies,
        key=lambda item: (
            identity_counts.get(str(item.get("identity") or ""), 0),
            item.get("mtime") if isinstance(item.get("mtime"), int) else -1,
            item.get("size") if isinstance(item.get("size"), int) else -1,
            str(item.get("host") or ""),
        ),
        reverse=True,
    )[0]


def _set_directory_canonical(action: RepairAction, canonical: dict[str, object] | None) -> None:
    if not canonical:
        return
    action.directory_canonical_host = str(canonical.get("host") or "")
    action.directory_canonical_backend = str(canonical.get("backend") or "")
    action.directory_canonical_gfid = str(
        canonical.get("backend_trusted_gfid") or canonical.get("gfid") or canonical.get("identity") or ""
    )


def _directory_child_gap_canonical_copy(
    obj: ManifestObject,
    child_names_by_host: dict[str, list[str]],
    missing_directory_children_by_host: dict[str, list[str]],
) -> dict[str, object] | None:
    copies = _directory_copy_records(obj)
    if not copies:
        return None
    gap_hosts = {
        host for host, missing_children in missing_directory_children_by_host.items() if missing_children
    }
    all_children = {child for names in child_names_by_host.values() for child in names if child}
    identity_counts: dict[str, int] = {}
    for item in copies:
        identity = str(item.get("identity") or "")
        if identity:
            identity_counts[identity] = identity_counts.get(identity, 0) + 1

    candidates: list[dict[str, object]] = []
    for item in copies:
        host = str(item.get("host") or "")
        if host in gap_hosts:
            continue
        host_children = set(child_names_by_host.get(host, []))
        if all_children and not all_children.issubset(host_children):
            continue
        candidates.append(item)
    if not candidates:
        candidates = [item for item in copies if str(item.get("host") or "") not in gap_hosts]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            len(child_names_by_host.get(str(item.get("host") or ""), [])),
            identity_counts.get(str(item.get("identity") or ""), 0),
            item.get("mtime") if isinstance(item.get("mtime"), int) else -1,
            item.get("size") if isinstance(item.get("size"), int) else -1,
            str(item.get("host") or ""),
        ),
        reverse=True,
    )[0]


def _directory_metadata_mismatch_hosts(obj: ManifestObject, canonical_gfid: str) -> list[str]:
    mismatch_hosts: list[str] = []
    if not canonical_gfid:
        return mismatch_hosts
    for host, observations in sorted(obj.observations.items()):
        if _is_arbiter_host(obj, host):
            continue
        for obs in observations:
            if not _directory_present(obs):
                continue
            if obs.backend_trusted_gfid != canonical_gfid:
                mismatch_hosts.append(host)
                break
    return mismatch_hosts


def _normalize_mdata_hex(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    if not cleaned:
        return ""
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    if not cleaned:
        return ""
    return f"0x{cleaned}"


def _directory_mdata_majority(
    obj: ManifestObject,
) -> tuple[str, list[str], list[str], Counter[str], dict[str, str]]:
    values_by_host: dict[str, str] = {}
    missing_hosts: list[str] = []
    for host, observations in sorted(obj.observations.items()):
        host_value = ""
        host_has_dir = False
        for obs in observations:
            if not _directory_present(obs):
                continue
            host_has_dir = True
            host_value = _normalize_mdata_hex(obs.backend_mdata_hex)
            if host_value:
                break
        if not host_has_dir:
            continue
        if host_value:
            values_by_host[host] = host_value
        else:
            missing_hosts.append(host)
    counts: Counter[str] = Counter(values_by_host.values())
    if not counts:
        return "", [], missing_hosts, counts, values_by_host
    canonical, canonical_count = sorted(
        counts.items(),
        key=lambda item: (item[1], item[0]),
        reverse=True,
    )[0]
    if canonical_count < _majority_threshold(obj):
        return "", [], missing_hosts, counts, values_by_host
    runner_up = sorted(counts.values(), reverse=True)[1] if len(counts) > 1 else 0
    if canonical_count <= runner_up:
        return "", [], missing_hosts, counts, values_by_host
    mismatch_hosts = [
        host
        for host in sorted(set(values_by_host) | set(missing_hosts))
        if values_by_host.get(host, "") != canonical
    ]
    return canonical, mismatch_hosts, missing_hosts, counts, values_by_host


def _directory_mdata_drift_present(obj: ManifestObject) -> bool:
    values: set[str] = set()
    missing = False
    for observations in obj.observations.values():
        for obs in observations:
            if not _directory_present(obs):
                continue
            value = _normalize_mdata_hex(obs.backend_mdata_hex)
            if value:
                values.add(value)
            else:
                missing = True
            break
    return len(values) > 1 or (bool(values) and missing)


def _directory_has_clear_majority_gfid(obj: ManifestObject, canonical_gfid: str) -> bool:
    if not canonical_gfid:
        return False
    copies = _directory_copy_records(obj)
    if not copies:
        return False
    identity_counts: Counter[str] = Counter(
        str(item.get("identity") or "") for item in copies if str(item.get("identity") or "")
    )
    canonical_count = identity_counts.get(canonical_gfid, 0)
    if canonical_count < _majority_threshold(obj):
        return False
    if len(identity_counts) == 1:
        return True
    runner_up = sorted(identity_counts.values(), reverse=True)[1]
    return canonical_count > runner_up


def _directory_child_gap_info(obj: ManifestObject) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    child_names_by_host: dict[str, list[str]] = {}
    for host, observations in sorted(obj.observations.items()):
        names: set[str] = set()
        seen_dir = False
        for obs in observations:
            if obs.backend_exists and obs.backend_lstat_type == "dir":
                seen_dir = True
                names.update(child for child in obs.backend_child_names if child)
        if seen_dir:
            child_names_by_host[host] = sorted(names)
    all_children = sorted({child for names in child_names_by_host.values() for child in names})
    missing_children_by_host = {
        host: sorted(set(all_children) - set(names))
        for host, names in child_names_by_host.items()
        if set(all_children) - set(names)
    }
    return child_names_by_host, missing_children_by_host


def _directory_child_gap_bounded_candidate(
    child_names_by_host: dict[str, list[str]],
    missing_directory_children_by_host: dict[str, list[str]],
    obj: ManifestObject | None = None,
) -> bool:
    if not child_names_by_host or not missing_directory_children_by_host:
        return False
    gap_hosts = [host for host, missing_children in missing_directory_children_by_host.items() if missing_children]
    if len(gap_hosts) != 1:
        return False
    missing_children = sorted(
        {
            child
            for missing_children in missing_directory_children_by_host.values()
            for child in missing_children
            if child
        }
    )
    if not missing_children:
        return False
    if any("/" in child for child in missing_children):
        return False
    return True


def _has_ambiguous_file_cohort_tie(obj: ManifestObject) -> bool:
    cohorts = _file_cohort_records(obj)
    if len(cohorts) < 2:
        return False
    top = len(cohorts[0]["hosts"])
    runner_up = len(cohorts[1]["hosts"])
    return top > 0 and top == runner_up


def _winner_identity(winner: ResolutionObservation) -> str:
    return _file_copy_identity(winner)


def _matches_winner(obs: ResolutionObservation, winner: ResolutionObservation) -> bool:
    if not (obs.backend_exists or (obs.backend_lexists and obs.backend_lstat_type == "symlink")):
        return False
    winner_identity = _winner_identity(winner)
    obs_identity = _file_copy_identity(obs)
    if winner_identity and obs_identity:
        return obs_identity == winner_identity
    return (
        obs.backend_size == winner.backend_size
        and obs.backend_mtime == winner.backend_mtime
    )


def _stage_local_path(logical_path: str) -> str:
    return default_stage_local_path(logical_path)


def _append_grouped_value(grouped: dict[str, list[str]], host: str, value: str) -> None:
    if not value:
        return
    values = grouped.setdefault(host, [])
    if value not in values:
        values.append(value)


def _resolve_manifest_reference(
    manifest: dict[str, ManifestObject],
    reference: str,
) -> ManifestObject | None:
    if not reference:
        return None
    candidates = [reference, reference.lstrip("/")]
    for logical_path, obj in manifest.items():
        if logical_path in candidates:
            return obj
        aliases = set(obj.aliases)
        aliases.add(logical_path)
        aliases.add(logical_path.lstrip("/"))
        if any(candidate in aliases for candidate in candidates):
            return obj
    return None


def _nested_gfid_child_residue(logical_path: str, obj: ManifestObject) -> bool:
    if not logical_path.startswith("unresolved-child:"):
        return False
    child_reference = logical_path.split(":", 1)[1]
    if child_reference.count("/") >= 2:
        return True
    return any(child_name.count("/") >= 1 for child_name in obj.child_names)


def _immediate_gfid_child_reference(logical_path: str, obj: ManifestObject) -> bool:
    if not logical_path.startswith("unresolved-child:"):
        return False
    child_reference = logical_path.split(":", 1)[1]
    return child_reference.count("/") == 1 and any(
        child_name and "/" not in child_name for child_name in obj.child_names
    )


def _regular_handle_ghost_candidate(obs: ResolutionObservation) -> tuple[str, str, str]:
    candidates = [
        ("gfid_path", obs.gfid_path, obs.gfid_path_lstat_type, obs.gfid_path_terminal_path or obs.gfid_path_readlink),
        ("backend", obs.backend, obs.backend_lstat_type, obs.backend_terminal_path or obs.backend_readlink),
    ]
    for source_label, path, kind, target in candidates:
        if path and "/.glusterfs/" in path and kind == "file" and target:
            return source_label, path, target
    return "", "", ""


def _nested_gfid_child_live_targets(
    logical_path: str,
    obj: ManifestObject,
    manifest: dict[str, ManifestObject],
) -> list[str]:
    if not _nested_gfid_child_residue(logical_path, obj):
        return []
    targets: list[str] = []
    for observations in obj.observations.values():
        for obs in observations:
            for candidate in (
                obs.gfid_path_terminal_path,
                obs.gfid_path_terminal_readlink,
                obs.backend_terminal_path,
                obs.backend_terminal_readlink,
            ):
                candidate = str(candidate or "").strip()
                if not candidate or "/.glusterfs/" in candidate or candidate.endswith("/.glusterfs"):
                    continue
                if _resolve_manifest_reference(manifest, candidate) and candidate not in targets:
                    targets.append(candidate)
    return targets


def _ancestor_paths(logical_path: str) -> list[str]:
    if not logical_path or ":" in logical_path:
        return []
    current = dirname(logical_path.rstrip("/"))
    ancestors: list[str] = []
    while current and current != "." and current != "/":
        ancestors.append(current)
        current = dirname(current)
    return list(reversed(ancestors))


def _scenario_root(logical_path: str) -> str:
    if not logical_path or ":" in logical_path:
        return ""
    return logical_path.split("/", 1)[0]


def _is_glusterfs_index_path(path: str) -> bool:
    return "/.glusterfs/indices/" in str(path or "")


def _is_bare_gfid_observation(obs: ResolutionObservation) -> bool:
    raw_entry = str(obs.raw_entry or "").strip()
    return raw_entry.startswith("<gfid:") and raw_entry.endswith(">") and "/" not in raw_entry


def _is_bare_gfid_raw_entry(raw_entry: str) -> bool:
    value = str(raw_entry or "").strip()
    return value.startswith("<gfid:") and value.endswith(">") and "/" not in value


def _object_is_bare_gfid_only(obj: ManifestObject) -> bool:
    return bool(obj.raw_entries) and all(_is_bare_gfid_raw_entry(entry) for entry in obj.raw_entries)


def _canonical_live_gfid_reference(obs: ResolutionObservation) -> str:
    if not _is_bare_gfid_observation(obs):
        return ""
    if not obs.gfid_path or _is_glusterfs_index_path(obs.gfid_path):
        return ""
    if obs.backend and obs.backend_exists:
        return obs.backend
    return ""
def _observed_live_reference_target(obs: ResolutionObservation) -> str:
    candidates = (
        (obs.gfid_path_terminal_path or obs.gfid_path_readlink, obs.gfid_path_terminal_exists),
        (obs.backend_terminal_path or obs.backend_readlink, obs.backend_terminal_exists),
    )
    for target, terminal_exists in candidates:
        # None preserves the conservative behavior for older manifests that
        # predate the explicit terminal-existence field.
        if target and terminal_exists is not False:
            return target
    return _canonical_live_gfid_reference(obs)



def _resolver_error_hosts(obj: ManifestObject) -> list[str]:
    hosts: list[str] = []
    for host, observations in sorted(obj.observations.items()):
        if not any(obs.error for obs in observations):
            continue
        has_evidence = any(
            obs.backend
            or obs.gfid_path
            or obs.file_gfid_path
            or obs.backend_exists
            or obs.gfid_exists
            for obs in observations
        )
        if not has_evidence:
            hosts.append(host)
    return hosts


def _resolver_error_notes(obj: ManifestObject) -> list[str]:
    notes: list[str] = []
    for host in _resolver_error_hosts(obj):
        errors = [obs.error for obs in obj.observations.get(host, []) if obs.error]
        if errors:
            notes.append(f"{host} resolver error: {errors[0]}")
    return notes


def _proofed_stale_index_paths_by_host(obj: ManifestObject) -> tuple[dict[str, list[str]], list[str]]:
    known_gfids = set(obj.dead_gfids) | set(obj.gfids)
    grouped: dict[str, list[str]] = {}
    live_references: list[str] = []
    if not known_gfids:
        return grouped, live_references
    for host, observations in sorted(obj.observations.items()):
        for obs in observations:
            if not _is_glusterfs_index_path(obs.gfid_path):
                continue
            if not obs.backend or not obs.backend_exists:
                continue
            if not obs.backend_trusted_gfid or obs.backend_trusted_gfid not in known_gfids:
                continue
            _append_grouped_value(grouped, host, obs.gfid_path)
            if obs.backend not in live_references:
                live_references.append(obs.backend)
    return grouped, sorted(live_references)


def _observed_stale_index_paths_by_host(obj: ManifestObject) -> tuple[dict[str, list[str]], list[str]]:
    """Return exact internal index paths that are present in live observations."""
    grouped: dict[str, list[str]] = {}
    errors: list[str] = []
    for host, observations in sorted(obj.observations.items()):
        for obs in observations:
            if not _is_glusterfs_index_path(obs.backend):
                continue
            if obs.error:
                errors.append(host)
                continue
            if obs.backend_exists:
                _append_grouped_value(grouped, host, obs.backend)
    return grouped, sorted(set(errors))


def _proofed_live_reference_target(obs: ResolutionObservation, known_gfids: set[str]) -> str:
    if (
        _is_glusterfs_index_path(obs.gfid_path)
        and obs.backend
        and obs.backend_trusted_gfid
        and obs.backend_trusted_gfid in known_gfids
    ):
        return obs.backend
    if (
        obs.gfid_path_terminal_path
        and obs.gfid_path_terminal_trusted_gfid
        and obs.gfid_path_terminal_trusted_gfid in known_gfids
    ):
        return obs.gfid_path_terminal_path
    if (
        obs.backend_terminal_path
        and obs.backend_terminal_trusted_gfid
        and obs.backend_terminal_trusted_gfid in known_gfids
    ):
        return obs.backend_terminal_path
    return ""


def build_plan(
    manifest: dict[str, ManifestObject],
    mountpoint: str,
    quorum_count: int = 2,
    split_brain_policy: str = "auto",
) -> list[PlanAction]:
    actions: list[PlanAction] = []
    action_map: dict[str, PlanAction] = {}

    logical_paths = sorted(
        manifest,
        key=lambda path: (
            0 if manifest[path].object_type == "file" else 1,
            -manifest[path].depth,
            path,
        ),
    )

    for logical_path in logical_paths:
        obj = manifest[logical_path]
        action_id = f"repair:{logical_path}"
        mounted_target = (
            ""
            if ":" in logical_path
            else f"{mountpoint.rstrip('/')}/{logical_path}".replace("//", "/")
        )

        stale_index_paths_by_host, stale_index_live_refs = _proofed_stale_index_paths_by_host(obj)
        # A bare-GFID heal row is normally safe to treat as stale index
        # bookkeeping only when it resolves to one file identity.  A
        # same-path/two-GFID row can have the same xattrop shape, but its
        # index entries are part of the split-brain evidence and must reach
        # the file conflict classifier.
        if (
            _object_is_bare_gfid_only(obj)
            and stale_index_paths_by_host
            and len(set(obj.file_gfids)) <= 1
            and "multiple-file-gfids-share-logical-path" not in obj.notes
            and "heal_info_marks_split_brain" not in obj.notes
        ):

            source_hosts_without_proof = sorted(set(obj.source_hosts) - set(stale_index_paths_by_host))
            resolver_error_hosts = _resolver_error_hosts(obj)
            can_cleanup_index = not source_hosts_without_proof and not resolver_error_hosts
            action = PlanAction(
                action_id=action_id,
                logical_path=logical_path,
                action_type="cleanup_stale_glusterfs_index" if can_cleanup_index else "review_dead_gfid_reference",
                object_type=obj.object_type,
                depth=obj.depth,
                repair_strategy=(
                    "delete_stale_glusterfs_index_residue" if can_cleanup_index else "follow_live_reference"
                ),
                mounted_target=mounted_target,
                raw_entries=obj.raw_entries,
                dead_gfids=obj.dead_gfids,
                dead_gfid_live_references=stale_index_live_refs,
                notes=list(obj.notes),
            )
            action.stale_backends_by_host = stale_index_paths_by_host
            action.stale_backends = sorted(
                {path for paths in stale_index_paths_by_host.values() for path in paths}
            )
            action.notes.append(
                "bare GFID heal row has stale .glusterfs index bookkeeping while the live backend keeps the matching trusted.gfid"
            )
            if stale_index_live_refs:
                action.notes.append("proofed live backend targets: " + ", ".join(stale_index_live_refs))
            if can_cleanup_index:
                action.notes.append(
                    "delete only the proven xattrop/dirty bookkeeping entries; do not remove the canonical .glusterfs GFID handle"
                )
            else:
                action.notes.append(
                    "stale index cleanup is review-only until every source host in heal info has a matching proof"
                )
                if source_hosts_without_proof:
                    action.notes.append(
                        "source hosts without stale-index proof: " + ", ".join(source_hosts_without_proof)
                    )
                action.notes.extend(_resolver_error_notes(obj))
            _attach_role_evidence(action, obj)
            actions.append(action)
            action_map[action_id] = action
            continue

        if obj.object_type == "file":
            winner = _arbiter_backed_data_file_winner(obj) or _best_file_copy(obj)
            present_hosts = _file_present_hosts(obj)
            present_count = len(present_hosts)
            quorum = _effective_content_quorum_threshold(obj, quorum_count)
            mount_accessible = _mounted_accessible(obj)
            action = PlanAction(
                action_id=action_id,
                logical_path=logical_path,
                action_type="repair_file",
                object_type=obj.object_type,
                depth=obj.depth,
                repair_strategy="replace_conflicting_file",
                mounted_target=mounted_target,
                raw_entries=obj.raw_entries,
                dead_gfids=obj.dead_gfids,
                stage_local_path=_stage_local_path(logical_path),
                restore_via_mount=True,
                file_copies=_file_copy_records(obj),
                file_cohorts=_file_cohort_records(obj),
                notes=list(obj.notes),
            )
            saw_symlink = (
                "saw_symlink" in obj.notes
                or "orphaned_symlink_present" in obj.notes
                or "file subtype: symlink" in obj.notes
            )
            if saw_symlink:
                action.notes.append("file subtype: symlink")
                _append_graph_marker(action, "file_subtype:symlink")
            if winner:
                action.winner_host = winner.host
                action.winner_backend = winner.backend
                action.winner_mtime = winner.backend_mtime
                action.winner_size = winner.backend_size
                action.winner_file_gfid = winner.backend_trusted_gfid or winner.file_gfid
            else:
                action.action_type = "cleanup_dead_file_refs"
                action.repair_strategy = "delete_dead_file_ref_residue"
                action.notes.append("no surviving file copy found")
                action.notes.append("delete the stale file residue after all live copies are ruled out")
            canonical_file_gfid = action.winner_file_gfid or ""
            file_metadata_mismatch_hosts = _file_metadata_mismatch_hosts(obj, canonical_file_gfid)
            action.file_metadata_backend_by_host = {
                host: obs.backend
                for host, observations in sorted(obj.observations.items())
                for obs in observations
                if _file_observation_is_present(obs) and obs.backend
            }
            action.file_target_backend_by_host = _file_target_backend_by_host(obj)
            source_host_count = len(obj.source_hosts)
            path_led_evidence = obj.source_hosts == ["localhost"]
            evidence_counts = [count for count in (present_count, source_host_count) if count > 0]
            quorum_evidence_count = present_count if path_led_evidence else (min(evidence_counts) if evidence_counts else 0)
            for host, observations in sorted(obj.observations.items()):
                host_is_arbiter = _is_arbiter_host(obj, host)
                host_has_match = False
                host_has_backend = False
                for obs in observations:
                    if winner and _matches_winner(obs, winner):
                        host_has_match = True
                        if not host_is_arbiter and host not in action.healthy_hosts:
                            action.healthy_hosts.append(host)
                        continue
                    if obs.backend_exists or (obs.backend_lexists and obs.backend_lstat_type == "symlink"):
                        host_has_backend = True
                        if obs.backend and obs.backend not in action.stale_backends:
                            action.stale_backends.append(obs.backend)
                            if host not in action.stale_hosts:
                                action.stale_hosts.append(host)
                            _append_grouped_value(action.stale_backends_by_host, host, obs.backend)
                        if obs.file_gfid_path and obs.file_gfid_path not in action.stale_file_gfid_paths:
                            action.stale_file_gfid_paths.append(obs.file_gfid_path)
                            _append_grouped_value(
                                action.stale_file_gfid_paths_by_host, host, obs.file_gfid_path
                            )
                    elif obs.gfid_path and obs.gfid_path not in action.stale_gfid_paths:
                        action.stale_gfid_paths.append(obs.gfid_path)
                        _append_grouped_value(action.stale_gfid_paths_by_host, host, obs.gfid_path)
                if host_has_match:
                    continue
                if host_is_arbiter:
                    continue
                if host_has_backend:
                    if host not in action.conflict_hosts:
                        action.conflict_hosts.append(host)
                else:
                    if host not in action.missing_hosts:
                        action.missing_hosts.append(host)
            arbiter_only_residue_hosts = _arbiter_only_residue_hosts(obj, object_type="file")
            if arbiter_only_residue_hosts:
                action.action_type = "cleanup_dead_file_refs"
                action.repair_strategy = "delete_dead_file_ref_residue"
                action.healthy_hosts = []
                action.missing_hosts = sorted(
                    host
                    for host in obj.observations
                    if _role_for_observation(obj, host) == "data"
                )
                _append_graph_marker(action, "arbiter_only_residue:file")
                action.notes.append(
                    "both data bricks prove the file absent; delete only the remaining arbiter backend, GFID handle, and index residue"
                )
                action.notes.append(
                    "arbiter-only file residue is metadata-only and must never be used as payload recovery evidence"
                )
                for note in _verification_notes(action.action_type, action.repair_strategy):
                    action.notes.append(note)
                _attach_role_evidence(action, obj)
                actions.append(action)
                action_map[action_id] = action
                continue
            arbiter_assisted_restore = _arbiter_supports_single_data_file_restore(
                obj,
                winner,
                present_hosts=present_hosts,
                missing_hosts=action.missing_hosts,
            )
            arbiter_assisted_conflict_resolution = _arbiter_supports_data_file_winner(
                obj,
                winner,
                conflict_hosts=action.conflict_hosts,
            )
            if arbiter_assisted_conflict_resolution:
                action.action_type = "review_entry_split_brain"
                action.repair_strategy = "arbiter_backed_data_identity_conflict"
                action.recommended_choice = "quarantine_loser"
                action.recommended_reason = (
                    "The matching data brick and arbiter form a strong identity quorum. Back up and quarantine the "
                    "conflicting data copy, then explicitly copy the matching data payload back to that brick."
                )
                _append_graph_marker(action, "file_arbiter_identity:authoritative_source")
                action.notes.append(
                    "one data brick and the arbiter agree on file identity; together they are strong authority to select that data brick as the payload source"
                )
                action.notes.append(
                    "the arbiter supplies identity/quorum evidence only; never read or copy arbiter payload"
                )
                action.notes.append(
                    "the recommended repair quarantines the conflicting data backend and GFID handle, then explicitly copies the matching data-brick payload; ordinary index heal is not the source-selection mechanism"
                )
                action.notes.append(
                    "choose the reversible repair, preserve both copies, or skip; verify from fresh evidence after any write"
                )
                for note in _verification_notes(action.action_type, action.repair_strategy):
                    action.notes.append(note)
                _attach_role_evidence(action, obj)
                actions.append(action)
                action_map[action_id] = action
                continue
            if (
                winner
                and not action.missing_hosts
                and not action.conflict_hosts
                and file_metadata_mismatch_hosts
            ):
                action.action_type = "repair_file_metadata"
                action.repair_strategy = "attach_file_gfid"
                action.file_metadata_mismatch_hosts = file_metadata_mismatch_hosts
                action.notes.append(
                    "file exists everywhere, but one or more bricks are missing the canonical trusted.gfid xattr"
                )
                action.notes.append(
                    f"metadata mismatch hosts: {', '.join(file_metadata_mismatch_hosts)}"
                )
                if canonical_file_gfid:
                    action.notes.append(f"canonical file GFID: {canonical_file_gfid}")
                action.native_heal_first = True
                action.native_heal_fallback_action = "repair_file_metadata"
                action.native_heal_reason = (
                    "The file already exists on all replicas and only metadata differs; try Gluster heal/rescan first, then rerun if the drift remains."
                )
            elif winner and not action.missing_hosts and not action.conflict_hosts:
                arbiter_gfid_hosts, arbiter_gfid_roots, arbiter_gfid_stale_paths = _arbiter_missing_gfid_hosts(
                    obj,
                    canonical_file_gfid,
                    object_type="file",
                )
                if arbiter_gfid_hosts:
                    action.action_type = "repair_file_metadata"
                    action.repair_strategy = "attach_file_gfid"
                    action.file_metadata_mismatch_hosts = arbiter_gfid_hosts
                    action.arbiter_gfid_repair_hosts = arbiter_gfid_hosts
                    action.arbiter_gfid_repair_backend_roots_by_host = arbiter_gfid_roots
                    action.arbiter_gfid_stale_paths_by_host = arbiter_gfid_stale_paths
                    action.notes.append(
                        "file exists everywhere, but the arbiter is missing, conflicts with, or lacks the checked canonical trusted.gfid/.glusterfs handle"
                    )
                    action.notes.append(f"canonical file GFID: {canonical_file_gfid}")
                    action.notes.append(
                        "both data bricks agree on file identity, so they authorize replacing a missing or conflicting arbiter trusted.gfid and rebuilding its checked .glusterfs handle"
                    )
                    action.notes.append(
                        "reattach the canonical GFID on the arbiter's own backend path and rebuild only its .glusterfs file handle"
                    )
            posix_metadata_by_host: dict[str, dict[str, object]] = {}
            posix_metadata_backend_by_host: dict[str, str] = {}
            for host, observations in sorted(obj.observations.items()):
                host_obs = next(
                    (
                        obs
                        for obs in observations
                        if obs.backend_exists and obs.backend_lstat_type == "file"
                    ),
                    None,
                )
                if host_obs is None:
                    continue
                posix_metadata_by_host[host] = _posix_metadata_record(host_obs)
                if host_obs.backend:
                    posix_metadata_backend_by_host[host] = host_obs.backend
            if (
                winner
                and not action.missing_hosts
                and not action.conflict_hosts
                and not file_metadata_mismatch_hosts
                and posix_metadata_by_host
                and len(posix_metadata_by_host) == _replica_count(obj)
            ):
                posix_signature_counts = Counter(
                    _posix_metadata_signature(record) for record in posix_metadata_by_host.values()
                )
                posix_majority_signature, posix_majority_count = posix_signature_counts.most_common(1)[0]
                posix_majority_hosts = [
                    host
                    for host, record in posix_metadata_by_host.items()
                    if _posix_metadata_signature(record) == posix_majority_signature
                ]
                posix_mismatch_hosts = [
                    host
                    for host, record in posix_metadata_by_host.items()
                    if _posix_metadata_signature(record) != posix_majority_signature
                ]
                if posix_mismatch_hosts:
                    source_host = posix_majority_hosts[0]
                    source_record = posix_metadata_by_host[source_host]
                    source_backend = posix_metadata_backend_by_host.get(source_host, "")
                    field_key_pairs = [
                        ("mode", "mode_bits"),
                        ("uid", "uid"),
                        ("gid", "gid"),
                        ("acl_access", "acl_access"),
                        ("acl_default", "acl_default"),
                    ]
                    fields_to_align = [
                        field_name
                        for field_name, record_key in field_key_pairs
                        if any(
                            posix_metadata_by_host[host][record_key] != source_record[record_key]
                            for host in posix_mismatch_hosts
                        )
                    ]
                    action.notes.append("metadata tuple by host: " + repr(posix_metadata_by_host))
                    if posix_majority_count > (_replica_count(obj) // 2):
                        action.action_type = "repair_posix_metadata"
                        action.repair_strategy = "align_posix_metadata_majority"
                        action.metadata_tuple_by_host = posix_metadata_by_host
                        action.metadata_backend_by_host = posix_metadata_backend_by_host
                        action.metadata_majority_hosts = posix_majority_hosts
                        action.metadata_mismatch_hosts = posix_mismatch_hosts
                        action.metadata_source_host = source_host
                        action.metadata_source_backend = source_backend
                        action.metadata_source_reason = "strict_majority"
                        action.metadata_fields_to_align = fields_to_align
                        action.notes.append(
                            "POSIX metadata is present on all replicas; align the minority bricks to the majority tuple"
                        )
                        action.notes.append(f"metadata source host: {source_host}")
                        action.notes.append(
                            "metadata majority hosts: " + ", ".join(posix_majority_hosts)
                        )
                        if fields_to_align:
                            action.notes.append(
                                "metadata fields to align: " + ", ".join(fields_to_align)
                            )
                        action.notes.append(
                            "metadata mismatch hosts: " + ", ".join(posix_mismatch_hosts)
                        )
                    else:
                        action.action_type = "review_posix_metadata_no_majority"
                        action.repair_strategy = "choose_posix_metadata_source"
                        action.metadata_tuple_by_host = posix_metadata_by_host
                        action.metadata_backend_by_host = posix_metadata_backend_by_host
                        action.metadata_majority_hosts = []
                        action.metadata_mismatch_hosts = sorted(posix_metadata_by_host)
                        action.metadata_fields_to_align = fields_to_align
                        action.gluster_visible_metadata_split_brain = (
                            "heal_info_marks_split_brain" in obj.notes
                        )
                        if action.gluster_visible_metadata_split_brain:
                            action.metadata_source_reason = "native_gluster_source"
                        action.notes.append(
                            "POSIX mode/uid/gid/ACL values differ; no strict majority exists, so choose the source tuple explicitly"
                        )
                        action.notes.append(
                            "candidate source hosts: " + ", ".join(sorted(posix_metadata_by_host))
                        )
                        if fields_to_align:
                            action.notes.append(
                                "metadata fields to align: " + ", ".join(fields_to_align)
                            )
                        action.notes.append(
                            "metadata mismatch hosts: " + ", ".join(action.metadata_mismatch_hosts)
                        )
                        if action.gluster_visible_metadata_split_brain:
                            action.notes.append(
                                "Gluster reports split-brain for this same-GFID object; verify checksums before choosing a metadata source, then use native source-brick resolution"
                            )
            half_present_no_majority = (
                winner is not None
                and _replica_count(obj) >= 2
                and _replica_count(obj) % 2 == 0
                and present_count * 2 == _replica_count(obj)
                and bool(action.missing_hosts)
                and not action.conflict_hosts
            )
            if half_present_no_majority and action.action_type not in {"repair_file_metadata", "repair_posix_metadata", "review_posix_metadata_no_majority"}:
                action.action_type = "review_entry_split_brain"
                action.repair_strategy = "ambiguous_file_presence_tie"
                action.recommended_choice = "quarantine_both"
                action.recommended_reason = (
                    "Exactly half of the replicas carry the file, so neither restore nor delete has strict-majority authority; preserve every visible copy before deciding."
                )
                _append_graph_marker(action, "file_presence_tie:review")
                action.notes.append(
                    f"file is present on exactly half of the replicas ({present_count} of {_replica_count(obj)}); client quorum does not provide strict-majority repair authority"
                )
                action.notes.append(
                    "do not infer restore or delete intent from the surviving cohort, even when every visible copy has the same GFID"
                )
                action.notes.append(
                    "preserve all visible copies with quarantine_both before an explicit restore-or-delete decision"
                )
            elif (
                0 < quorum_evidence_count < quorum
                and not arbiter_assisted_restore
                and not arbiter_assisted_conflict_resolution
                and action.action_type not in {"repair_file_metadata", "repair_posix_metadata", "review_posix_metadata_no_majority"}
            ):
                if saw_symlink:
                    action.action_type = "review_probable_orphaned_symlink"
                    action.repair_strategy = "delete_orphaned_symlink_residue"
                    _append_graph_marker(action, "file_subtype:symlink")
                    _append_graph_marker(action, f"mount:{'visible' if mount_accessible else 'hidden'}")
                    _append_graph_marker(
                        action,
                        f"file_orphaned_symlink:mount_{'visible' if mount_accessible else 'hidden'}",
                    )
                    action.notes.append(
                        f"symlink is present on only {quorum_evidence_count} of {_replica_count(obj)} replicas; below quorum threshold {quorum}"
                    )
                    if not mount_accessible:
                        action.notes.append(
                            "symlink is not accessible through the Gluster mount; likely orphaned residue after a failed delete"
                        )
                    else:
                        action.notes.append(
                            "symlink is still mount-accessible, but below-quorum residues should default toward delete review, not recreate"
                        )
                    action.notes.append(
                        "default batch policy should lean delete of the orphaned symlink backend and symlink/GFID metadata"
                    )
                else:
                    action.action_type = "review_probable_stale_survivor"
                    action.repair_strategy = "delete_below_quorum_file"
                    _append_graph_marker(action, "file_stale_survivor:review")
                    _append_graph_marker(action, f"mount:{'visible' if mount_accessible else 'hidden'}")
                    _append_graph_marker(
                        action,
                        f"file_stale_survivor:mount_{'visible' if mount_accessible else 'hidden'}",
                    )
                    action.notes.append(
                        f"file is present on only {quorum_evidence_count} of {_replica_count(obj)} replicas; below quorum threshold {quorum}"
                    )
                    if source_host_count and source_host_count < present_count:
                        action.notes.append(
                            f"heal entry source hosts: {', '.join(obj.source_hosts)}"
                        )
                    if not mount_accessible:
                        action.notes.append(
                            "file is not accessible through the Gluster mount; likely stale survivor after a failed delete"
                        )
                    else:
                        action.notes.append(
                            "file is still mount-accessible, but below-quorum survivors should default toward delete review, not recreate"
                        )
                    action.notes.append("default batch policy should lean delete of the stale backend file and file/GFID metadata")
            elif (
                not arbiter_assisted_conflict_resolution
                and action.action_type not in {"repair_posix_metadata", "review_posix_metadata_no_majority"}
                and (
                    _mount_access_problem(obj)
                    or len(obj.file_gfids) > 1
                    or "heal_info_marks_split_brain" in obj.notes
                )
            ):
                action.action_type = "review_entry_split_brain"
                if (
                    _has_ambiguous_file_cohort_tie(obj)
                    or "heal_info_marks_split_brain" in obj.notes
                ):
                    action.repair_strategy = "ambiguous_entry_split_brain_file"
                    if "heal_info_marks_split_brain" in obj.notes:
                        action.notes.append(
                            "heal info marks this file as split-brain; checksum visible copies before any source selection"
                        )
                    action.notes.append(
                        "file shows entry split-brain evidence, but the top file-identity cohorts are tied or heal marked the content split; do not auto-replace without checksum or explicit decision"
                    )
                    action.notes.append(
                        "decision file should specify keep_gfid or keep_host for this path"
                    )
                    if action.file_cohorts:
                        summary = ", ".join(
                            f"{item['identity']}:{len(item['hosts'])}"
                            for item in action.file_cohorts[:3]
                        )
                        action.notes.append(f"cohort summary: {summary}")
                else:
                    action.repair_strategy = "replace_entry_split_brain_file"
                    action.notes.append(
                        "file shows entry split-brain or name/GFID conflict evidence; try the official Gluster split-brain resolver first, then pick a winner, remove losing backend files and file/GFID metadata, and recreate through the mount if needed"
                    )
                    if _mount_access_problem(obj):
                        action.notes.append(
                            "file access through the Gluster mount reports an error or remains inaccessible despite backend presence; likely entry split-brain or name/GFID conflict"
                        )
            elif saw_symlink and "orphaned_symlink_present" in obj.notes and not any(
                _symlink_terminal_object(obs)[1]
                for observations in obj.observations.values()
                for obs in observations
            ):
                action.action_type = "review_probable_orphaned_symlink"
                action.repair_strategy = "delete_orphaned_symlink_residue"
                _append_graph_marker(action, "file_subtype:symlink")
                _append_graph_marker(action, "mount:hidden")
                _append_graph_marker(action, "file_orphaned_symlink:mount_hidden")
                action.notes.append(
                    "symlink residue is present on the volume, but the surviving inode does not look like a live file repair target"
                )
                action.notes.append(
                    "default batch policy should lean delete of the orphaned symlink backend and symlink/GFID metadata"
                )
            elif winner and action.action_type not in {"repair_file_metadata", "repair_posix_metadata", "review_posix_metadata_no_majority"}:
                child_gap_marker = any(
                    "file-child-gap" in note.lower() or "file child gap" in note.lower()
                    for note in obj.notes
                )
                if action.conflict_hosts:
                    action.repair_strategy = "replace_conflicting_file"
                    if arbiter_assisted_conflict_resolution:
                        action.restore_via_mount = False
                        _append_graph_marker(action, "file_replace:arbiter_assisted")
                        action.notes.append(
                            "one data brick and the arbiter agree on file identity; preserve the matching data payload, then remove only the conflicting data-brick residue"
                        )
                    action.notes.append("stage winning copy locally before cleanup")
                    if arbiter_assisted_conflict_resolution:
                        action.notes.append("backup and remove the conflicting backend and file-GFID residue only")
                        action.notes.append("after cleanup, run or observe native Gluster heal and rebuild the plan from fresh evidence")
                    else:
                        action.notes.append("delete conflicting backend copies before restore-through-mount")
                        action.notes.append("restore once through the mounted volume")
                elif action.missing_hosts and (
                    present_count >= quorum or arbiter_assisted_restore
                ) and len(action.missing_hosts) < quorum:
                    if child_gap_marker:
                        action.repair_strategy = "restore_missing_child_replica"
                        _append_graph_marker(action, "file_child_gap:present")
                        action.notes.append(
                            "file-child-gap marker present; keep the restore branch explicit so the child-gap variant can be tested and reasoned about separately"
                        )
                    else:
                        action.repair_strategy = "restore_missing_replica"
                    _append_graph_marker(action, "file_restore:review")
                    if arbiter_assisted_restore:
                        _append_graph_marker(action, "file_restore:arbiter_assisted")
                        action.notes.append(
                            "one data brick is missing while the other data brick and arbiter agree on file identity; restore only from the surviving data brick"
                        )
                    action.notes.append("stage winning copy locally")
                    action.notes.append("remove only stale metadata on missing replicas")
                    action.notes.append("delete the logical file through the mount if needed, then recreate it once from staged /tmp data to refill missing replicas")
                elif _file_identity_and_handles_are_verified(obj, canonical_file_gfid):
                    # This is an operator-path confirmation with no remaining
                    # repair work. Do not turn verified convergence into review.
                    continue
                else:
                    action.repair_strategy = "review_metadata_only"
                    action.action_type = "review_file_metadata"
                    _append_graph_marker(action, "file_metadata_only:fallback")
                    action.notes.append("no missing or conflicting backend file found; only metadata review remains")
            for note in _verification_notes(action.action_type, action.repair_strategy):
                action.notes.append(note)
            _attach_role_evidence(action, obj)
            actions.append(action)
            action_map[action_id] = action
            continue

        if obj.object_type in {"directory", "directory_candidate"}:
            depends_on = [
                f"repair:{child}"
                for child in obj.children
                if child in manifest
            ]
            present_hosts = _directory_present_hosts(obj)
            present_count = len(present_hosts)
            majority = _majority_threshold(obj)
            quorum = _effective_content_quorum_threshold(obj, quorum_count)
            mount_accessible = _mounted_accessible(obj)
            child_names_by_host, missing_directory_children_by_host = _directory_child_gap_info(obj)
            action = PlanAction(
                action_id=action_id,
                logical_path=logical_path,
                action_type="reconcile_directory",
                object_type=obj.object_type,
                depth=obj.depth,
                repair_strategy="reconcile_directory_set",
                mounted_target=mounted_target,
                raw_entries=obj.raw_entries,
                depends_on=depends_on,
                dead_gfids=obj.dead_gfids,
                directory_copies=_directory_copy_records(obj),
            )
            action.directory_child_names_by_host = child_names_by_host
            action.missing_directory_children_by_host = missing_directory_children_by_host
            action.directory_backend_by_host = {
                str(copy.get("host")): str(copy.get("backend"))
                for copy in action.directory_copies
                if str(copy.get("host") or "") and str(copy.get("backend") or "")
            }
            action.directory_target_backend_by_host = {
                str(host): str(observation.backend)
                for host, observations in sorted(obj.observations.items())
                for observation in observations
                if str(host) and str(observation.backend)
            }
            action.directory_target_backend_root_by_host = {
                str(host): _backend_root_for_observation(observation)
                for host, observations in sorted(obj.observations.items())
                for observation in observations
                if str(host) and observation.backend and _backend_root_for_observation(observation)
            }
            for host, observations in sorted(obj.observations.items()):
                host_has_dir = any(_directory_present(obs) for obs in observations)
                if host_has_dir:
                    action.healthy_hosts.append(host)
                else:
                    action.missing_hosts.append(host)
                for obs in observations:
                    if obs.gfid_path and not obs.backend_exists:
                        action.stale_gfid_paths.append(obs.gfid_path)
                        _append_grouped_value(action.stale_gfid_paths_by_host, host, obs.gfid_path)
                    if obs.backend_exists and obs.backend_lstat_type == "dir":
                        _append_grouped_value(action.stale_backends_by_host, host, obs.backend)
            arbiter_only_residue_hosts = _arbiter_only_residue_hosts(obj, object_type="directory")
            if arbiter_only_residue_hosts:
                action.action_type = "cleanup_arbiter_residue"
                action.repair_strategy = "delete_arbiter_only_residue"
                action.healthy_hosts = []
                action.missing_hosts = sorted(
                    host
                    for host in obj.observations
                    if _role_for_observation(obj, host) == "data"
                )
                for host in arbiter_only_residue_hosts:
                    for obs in obj.observations[host]:
                        handle_path = obs.backend_gfid_path or obs.gfid_path
                        if handle_path:
                            if handle_path not in action.stale_gfid_paths:
                                action.stale_gfid_paths.append(handle_path)
                            _append_grouped_value(action.stale_gfid_paths_by_host, host, handle_path)
                _append_graph_marker(action, "arbiter_only_residue:directory")
                action.notes.append(
                    "both data bricks prove the directory absent; delete only the remaining arbiter backend, GFID handle, and index residue"
                )
                action.notes.append(
                    "arbiter-only directory residue is namespace metadata, not a subtree recovery source"
                )
                for note in _verification_notes(action.action_type, action.repair_strategy):
                    action.notes.append(note)
                _attach_role_evidence(action, obj)
                actions.append(action)
                action_map[action_id] = action
                continue
            canonical = _directory_canonical_copy(obj)
            _set_directory_canonical(action, canonical)
            metadata_mismatch_hosts = _directory_metadata_mismatch_hosts(
                obj,
                action.directory_canonical_gfid,
            )
            arbiter_gfid_hosts, arbiter_gfid_roots, arbiter_gfid_stale_paths = _arbiter_missing_gfid_hosts(
                obj,
                action.directory_canonical_gfid,
                object_type="directory",
            )
            (
                canonical_mdata_hex,
                mdata_mismatch_hosts,
                missing_mdata_hosts,
                mdata_counts,
                mdata_by_host,
            ) = _directory_mdata_majority(obj)
            action.directory_mdata_by_host = dict(sorted(mdata_by_host.items()))
            directory_posix_by_host: dict[str, dict[str, object]] = {}
            directory_posix_backend_by_host: dict[str, str] = {}
            for host, observations in sorted(obj.observations.items()):
                host_obs = next((obs for obs in observations if _directory_present(obs)), None)
                if host_obs is None:
                    continue
                directory_posix_by_host[host] = _posix_metadata_record(host_obs)
                if host_obs.backend:
                    directory_posix_backend_by_host[host] = host_obs.backend
            directory_posix_majority_hosts: list[str] = []
            directory_posix_mismatch_hosts: list[str] = []
            directory_posix_fields_to_align: list[str] = []
            directory_posix_majority_count = 0
            if len(directory_posix_by_host) == _replica_count(obj):
                directory_posix_counts = Counter(
                    _posix_metadata_signature(record) for record in directory_posix_by_host.values()
                )
                directory_posix_signature, directory_posix_majority_count = directory_posix_counts.most_common(1)[0]
                directory_posix_majority_hosts = [
                    host for host, record in directory_posix_by_host.items()
                    if _posix_metadata_signature(record) == directory_posix_signature
                ]
                directory_posix_mismatch_hosts = [
                    host for host, record in directory_posix_by_host.items()
                    if _posix_metadata_signature(record) != directory_posix_signature
                ]
                if directory_posix_mismatch_hosts:
                    directory_source_record = directory_posix_by_host[directory_posix_majority_hosts[0]]
                    directory_posix_fields_to_align = [
                        field_name for field_name, record_key in [
                            ("mode", "mode_bits"),
                            ("uid", "uid"),
                            ("gid", "gid"),
                            ("acl_access", "acl_access"),
                            ("acl_default", "acl_default"),
                        ]
                        if any(
                            directory_posix_by_host[host][record_key] != directory_source_record[record_key]
                            for host in directory_posix_mismatch_hosts
                        )
                    ]
            tie_diff = bounded_tree_diff(
                obj,
                quorum_count=quorum_count,
            )
            if tie_diff.classification.branch_type != "stable" or tie_diff.budget.over_budget:
                action.notes.append(
                    f"directory tie preview: {tie_diff.classification.branch_type}; {tie_diff.planner_hint}"
                )
                if tie_diff.classification.branch_type == "gfid_conflict":
                    action.notes.append(
                        "directory tie prompt: run directory-tie-build to compare the bounded trees and choose merge, keep left/right, prune left/right, or defer"
                    )
                if tie_diff.budget.over_budget:
                    action.notes.append(
                        f"directory depth cap reached: {tie_diff.budget.stop_reason}"
                    )
            child_gap_hosts = sorted(
                host for host, missing_children in missing_directory_children_by_host.items() if missing_children
            )
            if arbiter_gfid_hosts and not action.missing_hosts and not child_gap_hosts:
                action.action_type = "repair_directory_metadata"
                action.repair_strategy = "attach_directory_gfid"
                action.directory_metadata_mismatch_hosts = arbiter_gfid_hosts
                action.arbiter_gfid_repair_hosts = arbiter_gfid_hosts
                action.arbiter_gfid_repair_backend_roots_by_host = arbiter_gfid_roots
                action.arbiter_gfid_stale_paths_by_host = arbiter_gfid_stale_paths
                action.notes.append(
                    "both data bricks agree on directory identity, so they authorize replacing a missing or conflicting arbiter trusted.gfid and rebuilding its checked .glusterfs handle"
                )
                action.notes.append(
                    "reattach the canonical GFID on the arbiter's own backend path and rebuild only its .glusterfs directory handle"
                )
            elif child_gap_hosts and not action.missing_hosts and not _directory_has_gfid_conflict(obj):
                action.notes.append("children must be repaired before parent directory")
                action.notes.append(
                    f"directory child-set reconciliation needed across hosts: {', '.join(_directory_hosts(obj))}"
                )
                child_gap_canonical = _directory_child_gap_canonical_copy(
                    obj,
                    child_names_by_host,
                    missing_directory_children_by_host,
                )
                if child_gap_canonical:
                    _set_directory_canonical(action, child_gap_canonical)
                action.action_type = "review_directory_children"
                action.repair_strategy = "reconcile_directory_children"
                _append_graph_marker(action, "directory_child_gap:present")
                action.notes.append(
                    "directory itself is present, but immediate child sets differ across bricks"
                )
                action.notes.append(
                    f"child-gap hosts: {', '.join(child_gap_hosts)}"
                )
                if child_gap_canonical:
                    action.notes.append(
                        "child-gap canonical source selected from a non-gap host with the complete child set: "
                        f"{action.directory_canonical_host}"
                    )
                for host, missing_children in sorted(missing_directory_children_by_host.items()):
                    if missing_children:
                        action.notes.append(
                            f"{host} is missing child entries: {', '.join(missing_children)}"
                        )
                action.notes.append(
                    "parent presence does not identify the missing child; repair concrete child actions first, then rerun manifest-build and plan-build so the parent directory can be reconsidered"
                )
                if _directory_child_gap_bounded_candidate(
                    child_names_by_host, missing_directory_children_by_host, obj
                ):
                    _append_graph_marker(action, "directory_child_gap:bounded_subtree_candidate")
                    action.stage_local_path = _stage_local_path(logical_path)
                    action.notes.append(
                        "bounded subtree candidate: keep this as an explicit follow-up branch; the live child-gap path stays at one immediate layer at a time"
                    )
                if tie_diff.classification.branch_type == "gfid_conflict":
                    _append_graph_marker(action, "directory_child_gap:split_brain_child")
            elif (
                _directory_has_gfid_conflict(obj)
                and action.directory_canonical_gfid
                and action.directory_canonical_backend
                and not action.missing_hosts
                and tie_diff.next_depth_matches
                and tie_diff.next_depth_signature
                and metadata_mismatch_hosts
            ):
                action.action_type = "repair_directory_metadata"
                action.repair_strategy = "attach_directory_gfid"
                action.directory_metadata_mismatch_hosts = metadata_mismatch_hosts
                action.notes.append(
                    "same logical directory name appears with multiple GFIDs, but the bounded trees collapse cleanly"
                )
                action.notes.append(
                    "merge can promote to the existing attach_directory_gfid path without recreating the directory tree"
                )
                action.notes.append(
                    f"merge-safe canonical directory GFID: {action.directory_canonical_gfid}"
                )
                action.notes.extend(_mount_access_error_notes(obj))
            elif _directory_has_gfid_conflict(obj):
                if (
                    action.directory_canonical_gfid
                    and _directory_has_clear_majority_gfid(obj, action.directory_canonical_gfid)
                    and metadata_mismatch_hosts
                    and not action.missing_hosts
                ):
                    action.action_type = "repair_directory_metadata"
                    action.repair_strategy = "attach_directory_gfid"
                    action.directory_metadata_mismatch_hosts = metadata_mismatch_hosts
                    action.notes.append(
                        "same logical directory name appears with multiple GFIDs, but one GFID has a clear majority"
                    )
                    action.notes.append(
                        "attach the canonical directory GFID to the minority bricks and keep the backup/snapshot path available"
                    )
                    action.notes.append(
                        f"majority-backed canonical directory GFID: {action.directory_canonical_gfid}"
                    )
                    action.notes.extend(_mount_access_error_notes(obj))
                else:
                    action.action_type = "review_directory_gfid_conflict"
                    action.repair_strategy = "rename_conflicting_directory"
                    action.recommended_choice = "quarantine_both"
                    action.recommended_reason = (
                        "Directory GFID cohorts conflict without a clear majority; preserve both trees before "
                        "choosing a merge, source, or pruning action."
                    )
                    action.notes.append(
                        "same logical directory name appears with multiple GFIDs; quarantine the losing backend entry before choosing a canonical directory"
                    )
                    action.notes.append(
                        "do not auto-merge or auto-delete directory conflicts in place"
                    )
                    action.notes.extend(_mount_access_error_notes(obj))
                    if action.missing_hosts:
                        action.notes.append(
                            f"some replicas also report the directory missing: {', '.join(action.missing_hosts)}"
                        )
            elif metadata_mismatch_hosts and not action.missing_hosts:
                action.action_type = "repair_directory_metadata"
                action.repair_strategy = "attach_directory_gfid"
                action.directory_metadata_mismatch_hosts = metadata_mismatch_hosts
                action.notes.append(
                    "directory exists everywhere, but one or more bricks are missing the canonical trusted.gfid xattr"
                )
                action.notes.append(
                    f"metadata mismatch hosts: {', '.join(metadata_mismatch_hosts)}"
                )
                if action.directory_canonical_gfid:
                    action.notes.append(
                        f"canonical directory GFID: {action.directory_canonical_gfid}"
                    )
            elif (
                canonical_mdata_hex
                and mdata_mismatch_hosts
                and not action.missing_hosts
                and not child_gap_hosts
                and not _directory_has_gfid_conflict(obj)
            ):
                action.action_type = "repair_directory_metadata"
                action.repair_strategy = "attach_directory_mdata"
                action.directory_canonical_mdata_hex = canonical_mdata_hex
                action.directory_mdata_mismatch_hosts = mdata_mismatch_hosts
                action.notes.append(
                    "directory exists everywhere and trusted.gfid/children agree, but trusted.glusterfs.mdata has a clear majority"
                )
                action.notes.append(f"majority directory mdata: {canonical_mdata_hex}")
                action.notes.append(f"mdata mismatch hosts: {', '.join(mdata_mismatch_hosts)}")
                if missing_mdata_hosts:
                    action.notes.append(
                        f"hosts missing trusted.glusterfs.mdata: {', '.join(missing_mdata_hosts)}"
                    )
                evidence = [
                    f"{host}={mdata_by_host.get(host) or 'missing'}"
                    for host in sorted(set(mdata_by_host) | set(missing_mdata_hosts))
                ]
                if evidence:
                    action.notes.append("mdata by host: " + ", ".join(evidence))
            elif (
                _directory_mdata_drift_present(obj)
                and not action.missing_hosts
                and not child_gap_hosts
                and not _directory_has_gfid_conflict(obj)
            ):
                action.action_type = "review_directory_metadata"
                action.repair_strategy = "review_directory_mdata_state"
                summary = ", ".join(
                    f"{value}:{count}" for value, count in sorted(mdata_counts.items())
                )
                action.notes.append(
                    "directory trusted.glusterfs.mdata differs, but no clear majority exists; operator must choose whether to align, let Gluster heal, or keep review"
                )
                if summary:
                    action.notes.append("mdata by value: " + summary)
                evidence = [
                    f"{host}={mdata_by_host.get(host) or 'missing'}"
                    for host in sorted(set(mdata_by_host) | set(missing_mdata_hosts))
                ]
                if evidence:
                    action.notes.append("mdata by host: " + ", ".join(evidence))
                action.native_heal_first = True
                action.native_heal_fallback_action = "repair_directory_metadata"
                action.native_heal_reason = (
                    "The directory trusted.glusterfs.mdata values differ without a clear majority; try Gluster heal/rescan first, then rerun if the metadata drift remains."
                )
            elif (
                directory_posix_mismatch_hosts
                and not action.missing_hosts
                and not child_gap_hosts
                and not _directory_has_gfid_conflict(obj)
                and directory_posix_majority_count > (_replica_count(obj) // 2)
            ):
                source_host = directory_posix_majority_hosts[0]
                action.action_type = "repair_posix_metadata"
                action.repair_strategy = "align_posix_metadata_majority"
                action.metadata_tuple_by_host = directory_posix_by_host
                action.metadata_backend_by_host = directory_posix_backend_by_host
                action.metadata_majority_hosts = directory_posix_majority_hosts
                action.metadata_mismatch_hosts = directory_posix_mismatch_hosts
                action.metadata_source_host = source_host
                action.metadata_source_backend = directory_posix_backend_by_host.get(source_host, "")
                action.metadata_source_reason = "strict_majority"
                action.metadata_fields_to_align = directory_posix_fields_to_align
                action.notes.append(
                    "POSIX metadata is present on all directory replicas; align the minority bricks to the majority tuple"
                )
                action.notes.append("metadata tuple by host: " + repr(directory_posix_by_host))
                action.notes.append(f"metadata source host: {source_host}")
                action.notes.append("metadata majority hosts: " + ", ".join(directory_posix_majority_hosts))
                action.notes.append("metadata fields to align: " + ", ".join(directory_posix_fields_to_align))
                action.notes.append("metadata mismatch hosts: " + ", ".join(directory_posix_mismatch_hosts))
            elif (
                directory_posix_mismatch_hosts
                and not action.missing_hosts
                and not child_gap_hosts
                and not _directory_has_gfid_conflict(obj)
            ):
                action.action_type = "review_posix_metadata_no_majority"
                action.repair_strategy = "choose_posix_metadata_source"
                action.metadata_tuple_by_host = directory_posix_by_host
                action.metadata_backend_by_host = directory_posix_backend_by_host
                action.metadata_majority_hosts = []
                action.metadata_mismatch_hosts = sorted(directory_posix_by_host)
                action.metadata_fields_to_align = directory_posix_fields_to_align
                action.notes.append(
                    "directory POSIX mode/uid/gid/ACL values differ without a strict majority; choose the source tuple explicitly"
                )
                action.notes.append("metadata tuple by host: " + repr(directory_posix_by_host))
                action.notes.append("candidate source hosts: " + ", ".join(sorted(directory_posix_by_host)))
            elif (
                _replica_count(obj) >= 2
                and _replica_count(obj) % 2 == 0
                and present_count * 2 == _replica_count(obj)
                and bool(action.missing_hosts)
            ):
                action.action_type = "review_directory_gfid_conflict"
                action.repair_strategy = "ambiguous_directory_presence_tie"
                action.recommended_choice = "quarantine_both"
                action.recommended_reason = (
                    "Exactly half of the replicas carry the directory, so neither restore nor subtree deletion has strict-majority authority; preserve every visible tree before deciding."
                )
                _append_graph_marker(action, "directory_presence_tie:review")
                action.notes.append(
                    f"directory is present on exactly half of the replicas ({present_count} of {_replica_count(obj)}); client quorum does not provide strict-majority repair authority"
                )
                action.notes.append(
                    "do not infer restore or subtree-delete intent from the surviving directory cohort, even when every visible copy has the same GFID"
                )
                action.notes.append(
                    "preserve all visible directory trees with quarantine_both before an explicit restore-or-delete decision"
                )
            elif 0 < present_count < quorum:
                action.action_type = "review_probable_stale_survivor"
                action.repair_strategy = "delete_below_quorum_subtree"
                action.notes.append(
                    f"directory is present on only {present_count} of {_replica_count(obj)} replicas; below quorum threshold {quorum}"
                )
                if not mount_accessible:
                    action.notes.append(
                        "directory is not accessible through the Gluster mount; likely stale survivor after a failed delete"
                    )
                else:
                    action.notes.append(
                        "directory is still mount-accessible, but below-quorum survivors should default toward subtree-delete review, not recreate"
                    )
                action.notes.append("default batch policy should lean delete of the stale subtree deepest-first, then parent last")
            elif _mount_access_problem(obj):
                action.action_type = "review_entry_split_brain"
                action.repair_strategy = "review_entry_split_brain_directory"
                action.notes.append(
                    "directory access through the Gluster mount reports an error or remains inaccessible despite backend presence; likely entry split-brain or name/GFID conflict"
                )
                action.notes.extend(_mount_access_error_notes(obj))
            elif action.missing_hosts and present_count >= quorum and len(action.missing_hosts) < quorum:
                directory_backend_child_gap_marker = any(
                    "directory-backend-child-gap" in note.lower()
                    or "directory backend child gap" in note.lower()
                    for note in obj.notes
                )
                if directory_backend_child_gap_marker:
                    action.action_type = "repair_directory_metadata"
                    action.repair_strategy = "recreate_missing_directory_backend_child_gap"
                    _append_graph_marker(action, "directory_backend_child_gap:present")
                    action.notes.append(
                        "directory-backend-child-gap marker present; keep the backend recreate branch explicit so the directory child-gap can be developed and tested separately"
                    )
                else:
                    action.action_type = "repair_directory_metadata"
                    action.repair_strategy = "recreate_missing_directory_backend"
                action.notes.append(
                    f"directory missing on replicas: {', '.join(action.missing_hosts)}"
                )
                action.notes.append(
                    "recreate the missing backend directory on the affected brick and restore the correct directory GFID linkage"
                )
                if canonical:
                    action.notes.append(
                        f"canonical directory copy appears on {action.directory_canonical_host} with backend {action.directory_canonical_backend}"
                    )
                    if action.directory_canonical_gfid:
                        action.notes.append(
                            f"canonical directory GFID: {action.directory_canonical_gfid}"
                        )
                if action.missing_hosts:
                    action.notes.append(
                        f"brick-side targets to recreate: {', '.join(action.missing_hosts)}"
                    )
            elif action.missing_hosts:
                action.action_type = "review_directory_metadata"
                action.repair_strategy = "review_directory_presence"
                action.notes.append("directory presence pattern does not cleanly fit recreate-vs-delete rules; review before action")
            elif action.stale_gfid_paths_by_host:
                action.repair_strategy = "cleanup_directory_metadata"
            elif action.depends_on:
                action.repair_strategy = "reconcile_directory_children"
            elif (
                _directory_identity_and_handles_are_verified(obj, action.directory_canonical_gfid)
                and not child_gap_hosts
                and not mdata_mismatch_hosts
                and not missing_mdata_hosts
            ):
                # This operator-path confirmation is fully converged. Do not
                # retain a review card merely because it was explicitly checked.
                continue
            else:
                action.repair_strategy = "review_directory_state"
                action.action_type = "review_directory_metadata"
                _append_graph_marker(action, "directory_metadata_only:fallback")
                action.notes.append("no missing directory or stale metadata found; review only")
            if action.stale_gfid_paths_by_host:
                action.notes.append("remove stale directory GFID metadata before parent restore")
            if action.directory_canonical_host and action.repair_strategy in {
                "recreate_missing_directory_backend",
                "recreate_missing_directory_backend_child_gap",
            }:
                action.notes.append(
                    "directory repair should be brick-side and GFID-aware, not a mount mkdir"
                )
            for note in _verification_notes(action.action_type, action.repair_strategy):
                action.notes.append(note)
            _attach_role_evidence(action, obj)
            actions.append(action)
            action_map[action_id] = action
            continue

        if obj.object_type == "stale_glusterfs_index":
            observed_index_paths_by_host, index_error_hosts = _observed_stale_index_paths_by_host(obj)
            can_cleanup_index = bool(observed_index_paths_by_host) and not index_error_hosts
            action = PlanAction(
                action_id=action_id,
                logical_path=logical_path,
                action_type="cleanup_stale_glusterfs_index" if can_cleanup_index else "review_dead_gfid_reference",
                object_type=obj.object_type,
                depth=obj.depth,
                repair_strategy=(
                    "delete_stale_glusterfs_index_residue" if can_cleanup_index else "refresh_stale_index_evidence"
                ),
                mounted_target=mounted_target,
                raw_entries=obj.raw_entries,
                dead_gfids=obj.dead_gfids,
                notes=list(obj.notes),
            )
            if not can_cleanup_index:
                action.recommended_choice = "refresh_evidence"
                action.recommended_reason = (
                    "No exact stale index path was confirmed on a brick; do not delete an operator-supplied path "
                    "until fresh evidence proves it exists."
                )
                action.notes.append(
                    "stale index cleanup is blocked because no exact internal index entry was observed on a brick"
                )
                if index_error_hosts:
                    action.notes.append("resolver errors while checking index paths: " + ", ".join(index_error_hosts))
                action.notes.append("skip this item or refresh read-only index evidence before replanning")
                _attach_role_evidence(action, obj)
                actions.append(action)
                action_map[action_id] = action
                continue
            action.notes.append(
                "internal Gluster heal bookkeeping entry; delete by default and do not back it up"
            )
            action.notes.append(
                "a later crawl or client access can recreate or rediscover the bookkeeping if it still matters"
            )
            action.notes.append("recurse one level into the internal index bucket before cleanup")
            for host, index_paths in sorted(observed_index_paths_by_host.items()):
                for index_root in index_paths:
                    observations = obj.observations.get(host, [])
                    matching_observations = [obs for obs in observations if obs.backend == index_root]
                    for obs in matching_observations:
                        child_names = [name for name in obs.backend_child_names if name]
                        if child_names:
                            action.notes.append(
                                f"{host} child entries: {', '.join(child_names)}"
                            )
                            for child_name in child_names:
                                index_target = f"{index_root.rstrip('/')}/{child_name}"
                                if index_target not in action.stale_backends:
                                    action.stale_backends.append(index_target)
                                if host not in action.stale_hosts:
                                    action.stale_hosts.append(host)
                                _append_grouped_value(action.stale_backends_by_host, host, index_target)
                        else:
                            if index_root not in action.stale_backends:
                                action.stale_backends.append(index_root)
                            if host not in action.stale_hosts:
                                action.stale_hosts.append(host)
                            _append_grouped_value(action.stale_backends_by_host, host, index_root)
            if not action.stale_backends_by_host:
                action.notes.append("no internal index paths were observed in the manifest; keep for review")
            _attach_role_evidence(action, obj)
            actions.append(action)
            action_map[action_id] = action
            continue

        if obj.object_type == "type_mismatch":
            file_copies = _file_copy_records(obj, include_arbiters=True)
            directory_copies = _directory_copy_records(
                obj,
                logical_path,
                include_arbiters=True,
            )
            recommended_choice, recommended_reason, mtime_summary, loser_side = (
                type_mismatch_quarantine_recommendation(
                    file_copies,
                    directory_copies,
                    expected_hosts=obj.observations,
                )
            )
            action = PlanAction(
                action_id=action_id,
                logical_path=logical_path,
                action_type="review_type_mismatch",
                object_type=obj.object_type,
                depth=obj.depth,
                mounted_target=mounted_target,
                raw_entries=obj.raw_entries,
                dead_gfids=obj.dead_gfids,
                file_copies=file_copies,
                directory_copies=directory_copies,
                recommended_choice=recommended_choice,
                recommended_reason=recommended_reason,
                type_mismatch_loser=loser_side,
                repair_strategy="review_type_mismatch",
                notes=list(obj.notes),
            )
            action.notes.append(f"type-mismatch mtime summary: {mtime_summary}")
            action.notes.append(f"type-mismatch recommended choice: {recommended_choice}")
            action.notes.append(f"type-mismatch recommended reason: {recommended_reason}")
            _append_graph_marker(action, "type_mismatch:review")
            _attach_role_evidence(action, obj)
            actions.append(action)
            action_map[action_id] = action
            continue

        is_nested_child_residue = obj.object_type == "unknown" and _nested_gfid_child_residue(logical_path, obj)
        is_immediate_child_reference = obj.object_type == "unknown" and _immediate_gfid_child_reference(logical_path, obj)
        is_gfid_child_reference = is_nested_child_residue or is_immediate_child_reference
        known_gfids = set(obj.dead_gfids) | set(obj.gfids)
        live_reference_targets = sorted(
            {
                _observed_live_reference_target(obs)
                for observations in obj.observations.values()
                for obs in observations
                if _observed_live_reference_target(obs)
            }
        )
        proofed_live_reference_targets = sorted(
            {
                target
                for observations in obj.observations.values()
                for obs in observations
                if (target := _proofed_live_reference_target(obs, known_gfids))
            }
        )
        action_type = "cleanup_dead_gfid" if (obj.object_type == "dead_gfid" or obj.dead_gfids) else "review"
        if obj.object_type == "unknown" and live_reference_targets:
            action_type = "review_dead_gfid_reference"
        action = PlanAction(
            action_id=action_id,
            logical_path=logical_path,
            action_type=action_type,
            object_type=obj.object_type,
            depth=obj.depth,
            mounted_target=mounted_target,
            raw_entries=obj.raw_entries,
            dead_gfids=obj.dead_gfids,
            dead_gfid_live_references=live_reference_targets,
            notes=list(obj.notes),
        )
        regular_handle_ghost_paths: list[str] = []
        regular_handle_ghost_sources: dict[str, list[str]] = {}
        for observations in obj.observations.values():
            for obs in observations:
                source_label, handle_path, handle_target = _regular_handle_ghost_candidate(obs)
                if not source_label or not handle_path or not handle_target:
                    continue
                if _resolve_manifest_reference(manifest, handle_target):
                    continue
                if handle_path not in action.stale_gfid_paths:
                    action.stale_gfid_paths.append(handle_path)
                _append_grouped_value(action.stale_gfid_paths_by_host, obs.host, handle_path)
                regular_handle_ghost_paths.append(handle_path)
                regular_handle_ghost_sources.setdefault(source_label, []).append(handle_path)
        if regular_handle_ghost_paths and not any("handle ghost" in note for note in action.notes):
            source_note = ", ".join(
                f"{label}: {', '.join(sorted(dict.fromkeys(paths)))}"
                for label, paths in sorted(regular_handle_ghost_sources.items())
            )
            action.notes.append(
                "regular .glusterfs handle ghost detected; safe to prune once its backlink no longer resolves"
                + (f" ({source_note})" if source_note else "")
            )
        if regular_handle_ghost_paths and not live_reference_targets and action.action_type == "review":
            action.action_type = "cleanup_dead_gfid"
            action.repair_strategy = "delete_dead_gfid_residue"
            _append_graph_marker(action, "dead_gfid:handle_ghost")
            action.notes.append(
                "regular handle ghost terminal target does not resolve to a live manifest object; cleanup is safe once the backlink target stays missing"
            )
            action.notes.append(
                "unknown object carries stale GFID residue but no live reference chain; cleanup is safe once the residue is unreferenced on all replicas"
            )
        if is_immediate_child_reference:
            action.action_type = "review_directory_children"
            action.repair_strategy = "verify_directory_child_absence"
            action.dead_gfids = []
            action.stale_gfid_paths = []
            action.stale_gfid_paths_by_host = {}
            _append_graph_marker(action, "directory_child_reference:immediate_missing")
            action.notes.append(
                "immediate GFID-child evidence resolves the parent directory, but no surviving child copy was observed"
            )
            if live_reference_targets:
                action.notes.append("parent directory targets: " + ", ".join(live_reference_targets))
            action.notes.append(
                "recover the child from an external backup if it should exist; otherwise confirm the deletion and remove only separately proven stale child/index residue"
            )
            action.notes.append(
                "do not delete the parent GFID handle and do not recreate an all-missing child without an authoritative source"
            )
        elif proofed_live_reference_targets and not is_gfid_child_reference:
            action.action_type = "cleanup_dead_gfid"
            action.repair_strategy = "delete_dead_gfid_residue"
            _append_graph_marker(action, "dead_gfid:stale_bookkeeping_proven")
            action.notes.append(
                "xattrop/dirty bookkeeping resolves to a live backend object with matching GFID; stale bookkeeping can be removed automatically"
            )
            action.notes.append("proofed live chain targets: " + ", ".join(proofed_live_reference_targets))
        elif is_nested_child_residue and live_reference_targets:
            resolved_targets: list[str] = []
            for reference in live_reference_targets:
                target_obj = _resolve_manifest_reference(manifest, reference)
                if not target_obj:
                    continue
                resolved_targets.append(
                    f"{reference} -> {target_obj.logical_path} ({target_obj.object_type})"
                )
            action.action_type = "review_directory_children"
            action.repair_strategy = "reconcile_directory_children"
            _append_graph_marker(action, "directory_child_reference:live")
            action.notes.append(
                "nested GFID-child residue has a live directory chain target; follow the live reference before cleanup"
            )
            action.notes.append("live chain targets: " + ", ".join(live_reference_targets))
            if resolved_targets:
                action.notes.append(
                    "resolved live reference chain: " + "; ".join(resolved_targets)
                )
        elif live_reference_targets:
            resolved_targets: list[str] = []
            for reference in live_reference_targets:
                target_obj = _resolve_manifest_reference(manifest, reference)
                if not target_obj:
                    continue
                resolved_targets.append(
                    f"{reference} -> {target_obj.logical_path} ({target_obj.object_type})"
                )
            if is_nested_child_residue:
                action.action_type = "review_directory_children"
                action.repair_strategy = "reconcile_directory_children"
                _append_graph_marker(action, "directory_child_reference:live")
                action.notes.append(
                    "nested GFID-child residue has a live directory chain target; follow the live reference before cleanup"
                )
                action.notes.append("live chain targets: " + ", ".join(live_reference_targets))
            else:
                action.action_type = "review_dead_gfid_reference"
                action.repair_strategy = "follow_live_reference"
                _append_graph_marker(action, "dead_gfid:live_reference")
                action.notes.append(
                    "dead GFID still resolves to live path reference(s): "
                    + ", ".join(live_reference_targets)
                )
                if resolved_targets:
                    action.notes.append(
                        "resolved live reference chain: " + "; ".join(resolved_targets)
                    )
                else:
                    action.notes.append(
                        "reference chain does not land in the current manifest; inspect the referenced live path before cleanup"
                    )
                action.notes.append(
                    "inspect the referenced live path before deciding whether the GFID residue is stale or part of a live conflict"
                )
                action.notes.append(
                    "if the live path already exists and xattrop/dirty bookkeeping is all that remains, keep the residue in review until a probe proves the terminal target matches the GFID"
                )
                action.notes.append(
                    "log the xattrop/entry-changes evidence and the terminal-path probe result for further investigation"
                )
        elif obj.object_type == "dead_gfid" or obj.dead_gfids:
            action.repair_strategy = "delete_dead_gfid_residue"
            _append_graph_marker(action, "dead_gfid:cleanup_terminal")
            action.notes.append("final-step-only")
            action.notes.append("confirm-unreferenced-on-all-replicas-before-delete")
            action.notes.append("rescan-after-low-hanging-fruit-repairs")
            action.notes.append("delete the stale GFID residue after all live references are ruled out")
        elif obj.object_type == "unknown" and _nested_gfid_child_residue(logical_path, obj):
            live_targets = _nested_gfid_child_live_targets(logical_path, obj, manifest)
            if live_targets:
                action.action_type = "review_directory_children"
                action.repair_strategy = "reconcile_directory_children"
                _append_graph_marker(action, "directory_child_reference:live")
                action.notes.append(
                    "nested GFID-child residue has a live directory chain target; follow the live reference before cleanup"
                )
                action.notes.append("live chain targets: " + ", ".join(live_targets))
            elif regular_handle_ghost_paths:
                action.action_type = "cleanup_dead_gfid"
                action.repair_strategy = "delete_dead_gfid_residue"
                _append_graph_marker(action, "dead_gfid:handle_ghost")
                action.notes.append(
                    "nested GFID-child residue is backed by a regular .glusterfs handle ghost; prune the stale handle before directory review"
                )
            else:
                action.action_type = "cleanup_dead_gfid"
                action.repair_strategy = "delete_dead_gfid_residue"
                _append_graph_marker(action, "dead_gfid:post_resolution_tail")
                action.notes.append(
                    "nested GFID-child residue does not resolve to a live directory chain target; treat it as a post-resolution ghost tail that is safe to prune once no live manifest object remains"
                )
                action.notes.append(
                    "inspect .glusterfs references and logs if the tail returns after cleanup"
                )
        for note in _verification_notes(action.action_type, action.repair_strategy):
            action.notes.append(note)
        for observations in obj.observations.values():
            for obs in observations:
                gfid_path_is_stale_residue = bool(
                    obs.gfid_path
                    and obs.gfid_path_lexists
                    and (
                        _is_glusterfs_index_path(obs.gfid_path)
                        or not obs.backend_exists
                        or not obs.backend
                    )
                )
                if (
                    gfid_path_is_stale_residue
                    and not is_immediate_child_reference
                    and obs.gfid_path not in action.stale_gfid_paths
                ):
                    action.stale_gfid_paths.append(obs.gfid_path)
                if gfid_path_is_stale_residue and not is_immediate_child_reference:
                    _append_grouped_value(action.stale_gfid_paths_by_host, obs.host, obs.gfid_path)
                if (
                    obs.gfid_path_terminal_path
                    and "/.glusterfs/" in obs.gfid_path_terminal_path
                    and obs.gfid_path_terminal_path not in action.stale_gfid_paths
                ):
                    action.stale_gfid_paths.append(obs.gfid_path_terminal_path)
                    _append_grouped_value(action.stale_gfid_paths_by_host, obs.host, obs.gfid_path_terminal_path)
                if (
                    obs.backend
                    and "/.glusterfs/" in obs.backend
                    and obs.backend_lstat_type == "file"
                    and (obs.backend_terminal_path or obs.backend_readlink)
                ):
                    handle_target = obs.backend_terminal_path or obs.backend_readlink
                    if not _resolve_manifest_reference(manifest, handle_target):
                        if obs.backend not in action.stale_gfid_paths:
                            action.stale_gfid_paths.append(obs.backend)
                            _append_grouped_value(action.stale_gfid_paths_by_host, obs.host, obs.backend)
                        if not any("gfid2path handle residue" in note for note in action.notes):
                            action.notes.append(
                                "gfid2path handle residue detected on a regular backend file; safe to prune once its backlink no longer resolves"
                            )
                if (
                    _valid_backend_reference(obs)
                    and obs.backend_lexists
                    and not obs.backend_exists
                    and obs.backend not in action.stale_backends
                ):
                    action.stale_backends.append(obs.backend)
                    _append_grouped_value(action.stale_backends_by_host, obs.host, obs.backend)
        if action.action_type == "review" and obj.object_type == "unknown" and (action.stale_gfid_paths or action.stale_backends):
            action.action_type = "cleanup_dead_gfid"
            action.repair_strategy = "delete_dead_gfid_residue"
            action.notes.append(
                "unknown object carries stale GFID residue but no live reference chain; cleanup is safe once the residue is unreferenced on all replicas"
            )
        if action.action_type == "cleanup_dead_gfid" and not (action.stale_gfid_paths or action.stale_backends):
            continue
        _attach_role_evidence(action, obj)
        actions.append(action)
        action_map[action_id] = action

    actions_by_scenario: dict[str, list[PlanAction]] = {}
    for action in actions:
        scenario_root = _scenario_root(action.logical_path)
        if scenario_root:
            actions_by_scenario.setdefault(scenario_root, []).append(action)

    restore_missing_dirs = {
        action.logical_path: action
        for action in actions
        if action.action_type in {"reconcile_directory", "repair_directory_metadata"}
        and action.repair_strategy == "recreate_missing_directory_backend"
    }
    for action in actions:
        if action.logical_path in restore_missing_dirs:
            action.depends_on = []
        ancestor_deps = []
        for ancestor in _ancestor_paths(action.logical_path):
            restore_action = restore_missing_dirs.get(ancestor)
            if restore_action and restore_action.action_id not in ancestor_deps:
                ancestor_deps.append(restore_action.action_id)
        if ancestor_deps:
            existing = list(action.depends_on)
            for dep in ancestor_deps:
                if dep not in existing:
                    existing.append(dep)
            action.depends_on = existing

    for action in actions:
        if action.action_type != "review_directory_children":
            continue
        if action.repair_strategy == "verify_directory_child_absence":
            continue
        dependency_types: Counter[str] = Counter()
        blockers: Counter[str] = Counter()
        promotable_dependencies: list[str] = []
        soft_dependencies: list[str] = []
        for dep_id in action.depends_on:
            dep = action_map.get(dep_id)
            dep_type = dep.action_type if dep else "MISSING"
            dependency_types[dep_type] += 1
            dep_promotable = dep is not None and _directory_child_dependency_is_promotable(
                dep,
                split_brain_policy=split_brain_policy,
            )
            if dep_promotable:
                promotable_dependencies.append(dep_id)
            elif dep is not None and _directory_child_dependency_is_soft(dep):
                soft_dependencies.append(dep_id)
            if (
                dep is None
                or dep_type == "defer_dead_gfid_cleanup"
                or (
                    dep is not None
                    and dep_type.startswith("review_")
                    and not _directory_child_dependency_is_soft(dep)
                    and not dep_promotable
                )
            ):
                blockers[dep_type] += 1
        if dependency_types:
            summary = ", ".join(f"{kind}={count}" for kind, count in sorted(dependency_types.items()))
            action.notes.append(f"child-gap dependency summary: {summary}")
        if soft_dependencies:
            action.notes.append(
                "child-gap soft dependencies do not supply a structural child repair: "
                + ", ".join(soft_dependencies)
            )
        if blockers:
            summary = ", ".join(f"{kind}={count}" for kind, count in sorted(blockers.items()))
            action.notes.append(f"child-gap blockers: {summary}")
        elif promotable_dependencies:
            action.depends_on = promotable_dependencies
            action.action_type = "reconcile_directory"
            _append_graph_marker(action, "directory_child_gap:executable")
            action.notes.append(
                "child-gap dependency closure is executable; parent directory can be reconsidered after child repairs and a fresh rescan"
            )
        else:
            action.depends_on = []
            action.notes.append(
                "parent presence alone does not prove child intent or provide the missing child's GFID/type"
            )
            action.notes.append(
                "collect immediate --gfid-child <parent-gfid>/<child> evidence for each missing child, with mount probing off, then rebuild the plan"
            )

    for action in actions:
        scenario_root = _scenario_root(action.logical_path)
        if not scenario_root:
            continue
        related_actions = actions_by_scenario.get(scenario_root, [])
        if action.action_type == "review_probable_stale_survivor":
            followups = [
                other.logical_path
                for other in related_actions
                if other.logical_path != action.logical_path
                and other.action_type in {
                    "review_entry_split_brain",
                    "review_directory_children",
                    "review_directory_metadata",
                    "reconcile_directory",
                }
            ]
        elif action.action_type in {
            "review_entry_split_brain",
            "review_directory_children",
            "review_directory_metadata",
            "reconcile_directory",
        }:
            prior_file_residue = [
                other.logical_path
                for other in related_actions
                if other.logical_path != action.logical_path
                and other.action_type == "review_probable_stale_survivor"
            ]

    annotate_execution_waves(actions)
    annotate_plan(actions)
    return actions
