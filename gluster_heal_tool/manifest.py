# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Manifest building helpers."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .models import HealEntry, ManifestObject, ResolutionObservation
from .role_safety import normalize_brick_host_aliases, normalize_brick_roles
from .shared_io import write_json_shared


def _normalize_logical_path(path: str) -> str:
    value = path.strip()
    if value.startswith("/"):
        value = value[1:]
    while "//" in value:
        value = value.replace("//", "/")
    return value


def _logical_key(entry: HealEntry, observations: list[ResolutionObservation]) -> str:
    relpaths = sorted({_normalize_logical_path(obs.relpath) for obs in observations if obs.relpath})
    if relpaths:
        return relpaths[0]
    if entry.kind == "path":
        return _normalize_logical_path(entry.raw)
    if entry.kind == "gfid_child" and entry.parent_gfid and entry.child_name:
        return f"unresolved-child:{entry.parent_gfid}/{entry.child_name}"
    if entry.parent_gfid:
        return f"dead-gfid:{entry.parent_gfid}"
    return f"unresolved:{entry.raw}"


def _object_type(entry: HealEntry, observations: list[ResolutionObservation], logical_key: str) -> str:
    if logical_key.startswith("dead-gfid:"):
        return "dead_gfid"
    if logical_key.startswith(".glusterfs/indices/"):
        return "stale_glusterfs_index"
    kinds = {obs.backend_lstat_type for obs in observations if obs.backend_lstat_type}
    terminal_kinds = {
        obs.backend_terminal_lstat_type
        for obs in observations
        if obs.backend_terminal_lstat_type and not _is_internal_path(obs.backend_terminal_path)
    } | {
        obs.gfid_path_terminal_lstat_type
        for obs in observations
        if obs.gfid_path_terminal_lstat_type and not _is_internal_path(obs.gfid_path_terminal_path)
    }
    symlink_obs = [obs for obs in observations if obs.backend_lstat_type == "symlink"]
    if "file" in kinds and "dir" in kinds:
        return "type_mismatch"
    if "file" in kinds:
        return "file"
    if "dir" in kinds:
        return "directory"
    if "file" in terminal_kinds and "dir" in terminal_kinds:
        return "type_mismatch"
    if "dir" in terminal_kinds:
        return "directory"
    if "file" in terminal_kinds:
        return "file"
    if symlink_obs:
        return "file"
    if entry.kind == "path" and "." not in logical_key.rsplit("/", 1)[-1]:
        return "directory_candidate"
    return "unknown"


def _is_internal_path(path: str) -> bool:
    return "/.glusterfs/" in path or path.endswith("/.glusterfs")


def build_manifest(
    heal_entries: list[HealEntry],
    resolver,
    brick_roles_by_host: dict[str, str] | None = None,
    brick_host_aliases: dict[str, list[str]] | None = None,
    brick_role_evidence_required: bool = False,
    brick_role_evidence_error: str = "",
) -> tuple[dict[str, ManifestObject], list[ResolutionObservation]]:
    manifest: dict[str, ManifestObject] = {}
    normalized_brick_roles = normalize_brick_roles(brick_roles_by_host)
    normalized_brick_host_aliases = normalize_brick_host_aliases(brick_host_aliases)
    all_observations: list[ResolutionObservation] = []
    children_by_parent: dict[str, set[str]] = defaultdict(set)

    for entry in heal_entries:
        observations = resolver.resolve_entry(entry.raw)
        all_observations.extend(observations)
        logical_key = _logical_key(entry, observations)
        depth = max(
            [obs.depth for obs in observations if obs.depth]
            + ([logical_key.count("/") + 1] if logical_key and ":" not in logical_key else [0])
        )
        obj = manifest.setdefault(
            logical_key,
            ManifestObject(
                logical_path=logical_key,
                object_type=_object_type(entry, observations, logical_key),
                depth=depth,
                input_source=entry.input_source,
                brick_roles_by_host=normalized_brick_roles,
                brick_host_aliases=normalized_brick_host_aliases,
                brick_role_evidence_required=brick_role_evidence_required,
                brick_role_evidence_error=brick_role_evidence_error,
            ),
        )
        obj.depth = max(obj.depth, depth)
        if obj.input_source == "heal_info" and entry.input_source != "heal_info":
            obj.input_source = entry.input_source
        if entry.raw not in obj.raw_entries:
            obj.raw_entries.append(entry.raw)
        if entry.split_brain and "heal_info_marks_split_brain" not in obj.notes:
            obj.notes.append("heal_info_marks_split_brain")
        if entry.source_host not in obj.source_hosts:
            obj.source_hosts.append(entry.source_host)
        if entry.parent_gfid and entry.parent_gfid not in obj.parent_gfids:
            obj.parent_gfids.append(entry.parent_gfid)
        if entry.parent_gfid and entry.parent_gfid not in obj.gfids:
            obj.gfids.append(entry.parent_gfid)
        if logical_key.startswith("dead-gfid:") and entry.parent_gfid and entry.parent_gfid not in obj.dead_gfids:
            obj.dead_gfids.append(entry.parent_gfid)
        if entry.child_name and entry.child_name not in obj.child_names:
            obj.child_names.append(entry.child_name)
        if not observations:
            obj.unresolved_entries.append(entry.raw)

        for obs in observations:
            host_list = obj.observations.setdefault(obs.host, [])
            obs_key = (
                obs.host,
                obs.raw_entry,
                obs.backend,
                obs.gfid_path,
                obs.file_gfid_path,
                obs.backend_lstat_type,
                obs.error,
            )
            existing_keys = {
                (
                    item.host,
                    item.raw_entry,
                    item.backend,
                    item.gfid_path,
                    item.file_gfid_path,
                    item.backend_lstat_type,
                    item.error,
                )
                for item in host_list
            }
            if obs_key not in existing_keys:
                host_list.append(obs)
            if obs.gfid and obs.gfid not in obj.gfids:
                obj.gfids.append(obs.gfid)
            if obs.file_gfid and obs.file_gfid not in obj.file_gfids:
                obj.file_gfids.append(obs.file_gfid)
            if obs.backend and obs.backend not in obj.aliases:
                obj.aliases.append(obs.backend)
            if obs.gfid_path and obs.gfid_path not in obj.aliases:
                obj.aliases.append(obs.gfid_path)
            if obs.relpath and _normalize_logical_path(obs.relpath) != logical_key:
                note = f"alternate-relpath:{_normalize_logical_path(obs.relpath)}"
                if note not in obj.notes:
                    obj.notes.append(note)
            if (
                logical_key.startswith("dead-gfid:")
                and obs.gfid
                and obs.gfid not in obj.dead_gfids
            ):
                obj.dead_gfids.append(obs.gfid)
            if obs.error and "same file name" in obs.error.lower():
                if "dir_name_gfid_conflict" not in obj.notes:
                    obj.notes.append("dir_name_gfid_conflict")
            if obs.backend_lstat_type in {"file", "dir", "symlink"}:
                type_note = (
                    "saw_dir"
                    if obs.backend_lstat_type == "dir"
                    else "saw_file"
                    if obs.backend_lstat_type == "file"
                    else "saw_symlink"
                )
                if type_note not in obj.notes:
                    obj.notes.append(type_note)
            if obs.gfid_path_is_symlink and "saw_gfid_symlink" not in obj.notes:
                obj.notes.append("saw_gfid_symlink")
            if obs.backend_terminal_trusted_gfid and obs.backend_terminal_trusted_gfid not in obj.file_gfids:
                obj.file_gfids.append(obs.backend_terminal_trusted_gfid)
            if obs.gfid_path_terminal_trusted_gfid and obs.gfid_path_terminal_trusted_gfid not in obj.file_gfids:
                obj.file_gfids.append(obs.gfid_path_terminal_trusted_gfid)
            if obs.backend_trusted_gfid and obs.backend_trusted_gfid not in obj.file_gfids:
                obj.file_gfids.append(obs.backend_trusted_gfid)
            if obs.backend_lexists and not obs.backend_exists and obs.backend_is_symlink:
                if "orphaned_symlink_present" not in obj.notes:
                    obj.notes.append("orphaned_symlink_present")
            if obs.mounted_checked and obs.mounted_error:
                mount_note = f"mount_access_error:{obs.host}"
                if mount_note not in obj.notes:
                    obj.notes.append(mount_note)
            if obs.mounted_checked and not obs.mounted_exists and obs.backend_exists:
                mount_note = f"mount_missing_while_backend_present:{obs.host}"
                if mount_note not in obj.notes:
                    obj.notes.append(mount_note)

        if "/" in logical_key and not logical_key.startswith(("dead-gfid:", "unresolved:")):
            parent = logical_key.rsplit("/", 1)[0]
            children_by_parent[parent].add(logical_key)

    for logical_path, obj in manifest.items():
        obj.children = sorted(children_by_parent.get(logical_path, set()))
        if len(obj.gfids) > 1:
            obj.notes.append("multiple-gfids-share-logical-path")
        if len(obj.file_gfids) > 1 and "multiple-file-gfids-share-logical-path" not in obj.notes:
            obj.notes.append("multiple-file-gfids-share-logical-path")
        if logical_path.startswith("dead-gfid:"):
            obj.object_type = "dead_gfid"
        elif "saw_file" in obj.notes and "saw_dir" in obj.notes:
            obj.object_type = "type_mismatch"
            obj.notes.append("file_dir_type_mismatch")
        if obj.object_type == "unknown" and obj.children:
            obj.object_type = "directory_candidate"

    return manifest, all_observations


def write_manifest(path: str | Path, manifest: dict[str, ManifestObject], *,
                   volume: str = "", bricks: list[dict] | None = None) -> None:
    payload = {
        "schema_version": 1,
        "objects": {
            logical_path: obj.to_dict()
            for logical_path, obj in sorted(manifest.items())
        },
    }
    if volume and bricks and all(item.get("role") in {"data", "arbiter"} for item in bricks):
        from .apply_binding import bind_manifest
        bind_manifest(payload, volume=volume, bricks=bricks)
    write_json_shared(path, payload)


def load_manifest(path: str | Path, *, payload: dict | None = None) -> dict[str, ManifestObject]:
    if payload is None:
        payload = json.loads(Path(path).read_text())
    objects = payload.get("objects", {})
    manifest: dict[str, ManifestObject] = {}
    for logical_path, raw in objects.items():
        observations = {
            host: [ResolutionObservation(**item) for item in values]
            for host, values in raw.get("observations", {}).items()
        }
        manifest[logical_path] = ManifestObject(
            logical_path=raw["logical_path"],
            object_type=raw["object_type"],
            depth=raw["depth"],
            input_source=raw.get("input_source", "heal_info"),
            raw_entries=raw.get("raw_entries", []),
            source_hosts=raw.get("source_hosts", []),
            parent_gfids=raw.get("parent_gfids", []),
            file_gfids=raw.get("file_gfids", []),
            gfids=raw.get("gfids", []),
            child_names=raw.get("child_names", []),
            observations=observations,
            dead_gfids=raw.get("dead_gfids", []),
            unresolved_entries=raw.get("unresolved_entries", []),
            aliases=raw.get("aliases", []),
            notes=raw.get("notes", []),
            children=raw.get("children", []),
            brick_roles_by_host=normalize_brick_roles(raw.get("brick_roles_by_host")),
            brick_host_aliases=normalize_brick_host_aliases(raw.get("brick_host_aliases")),
            brick_role_evidence_required=bool(raw.get("brick_role_evidence_required")),
            brick_role_evidence_error=str(raw.get("brick_role_evidence_error") or ""),
        )
    return manifest
