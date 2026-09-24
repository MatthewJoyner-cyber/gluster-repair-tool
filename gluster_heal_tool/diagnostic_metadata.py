# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Versioned, lossy diagnostic projections. Never copy arbitrary evidence text.

Schemas describe each container explicitly. Unknown keys, free text and values
of the wrong type are omitted, including keys inside caller-controlled maps.
Aliases are local to one bundle; the reverse mapping is never serialized.
"""
from __future__ import annotations

import json
import re


GFID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
TYPES = frozenset({"", "unknown", "file", "directory", "symlink", "missing",
                   "dead_gfid", "stale_glusterfs_index", "other"})
ROLES = frozenset({"data", "arbiter", "unknown", ""})
VOLUME_TYPES = frozenset({"Replicate", "Distributed-Replicate", "Disperse",
                          "Distributed-Disperse", "Distributed", ""})
VOLUME_STATES = frozenset({"Started", "Stopped", ""})
STATES = frozenset({"", "unknown", "planned", "proposed", "review", "skipped",
                    "blocked", "running", "completed", "executed", "failed",
                    "interrupted", "pending", "not-started", "ready"})
ACTIONS = frozenset({
    "repair_file", "repair_file_metadata", "repair_posix_metadata",
    "repair_directory_metadata", "reconcile_directory", "clear_split_brain_marker",
    "cleanup_dead_gfid", "cleanup_dead_file_refs", "cleanup_stale_glusterfs_index",
    "cleanup_arbiter_residue", "cleanup_orphaned_symlink", "review_entry_split_brain",
    "review_probable_stale_survivor", "review_probable_orphaned_symlink",
    "review_directory_children", "review_directory_gfid_conflict",
    "review_directory_metadata", "review_file_metadata", "review_dead_gfid_reference",
    "review_type_mismatch", "review_posix_metadata_no_majority", "defer_dead_gfid_cleanup",
})
STEPS = frozenset({
    "resolve_split_brain_gluster_cli", "mkdir_stage_parent", "mkdir_mount_parent",
    "ensure_directory_via_mount", "remove_restored_mount_file", "restore_via_mount",
    "restore_via_mount_child_gap", "restore_file_backend_gap_fill",
    "stage_directory_subtree_local", "mkdir_directory_backend",
    "mkdir_directory_backend_child_gap", "attach_directory_gfid", "attach_directory_mdata",
    "quarantine_directory_backend", "quarantine_directory_gfid", "quarantine_file_backend",
    "quarantine_file_gfid", "attach_file_gfid",
    "stage_winner_local", "backup_stale_backend", "remove_stale_backend",
    "restore_backend_backup", "backup_stale_file_gfid", "remove_stale_file_gfid",
    "restore_file_gfid_backup", "backup_stale_gfid", "remove_stale_gfid",
    "restore_gfid_backup", "restore_stage_backup_locally", "relink_arbiter_file_gfid",
    "quarantine_stale_arbiter_file_gfid", "restore_quarantined_stale_arbiter_file_gfid",
    "apply_posix_metadata_owner", "apply_posix_metadata_mode", "apply_posix_metadata_acl",
    "wait_for_child_repairs", "cleanup_directory_metadata", "backup_stale_dir_gfid",
    "remove_stale_dir_gfid", "restore_dir_gfid_backup", "verify_directory_backend",
    "backup_directory_subtree", "restore_directory_subtree_bounded",
    "verify_directory_backend_child_gap", "restore_directory_subtree_backup",
    "relink_arbiter_directory_gfid", "quarantine_stale_arbiter_directory_gfid",
    "restore_quarantined_stale_arbiter_directory_gfid", "restore_quarantined_directory_gfid",
    "restore_quarantined_directory", "restore_quarantined_file_backend",
    "restore_quarantined_file_gfid", "review_entry_split_brain_directory",
    "review_arbiter_data_identity_conflict", "review_heal_marked_content_split",
    "review_file_presence_tie", "review_ambiguous_entry_split_brain",
    "review_probable_stale_subtree", "review_stale_glusterfs_index_cleanup",
    "review_action", "review_directory_revert", "review_directory_state",
    "review_dead_gfid_cleanup", "review_dead_file_ref_cleanup",
}) | ACTIONS
HOST = ("alias", "server")
PATH = ("alias", "path")
ACTION_ID = ("alias", "action")
ROLE_MAP = ("map", HOST, ROLES)
HOST_ALIASES = ("map", HOST, ("list", HOST))
BRICK = {"host": HOST, "path": PATH, "role": ROLES}
BINDING = {"origin": {"volume": ("alias", "volume"), "bricks": ("list", BRICK)}}
HEALTH_CHECK_KINDS = frozenset({
    "volume_type", "heal_settings", "snapshot_inventory", "volume_status",
    "ssh_reachable", "host_identity", "worker_available", "host_ops_available",
    "log_ops_available", "brick_mount", "brick_free_space", "brick_free_inodes",
    "glusterd", "heal_activity", "heal_lock_skip_probe", "self_heal_daemon_status",
})
HEALTH_CHECK = {
    "kind": HEALTH_CHECK_KINDS, "ok": "boolean", "host": HOST, "path": PATH,
    "online": "boolean", "pid_running": "boolean", "available": "boolean",
    "busy": "boolean", "inspected_glusterfsd": "boolean", "returncode": "integer",
    "count": "integer", "match_count": "integer", "available_bytes": "integer",
    "available_inodes": "integer",
}
HEALTH_BRICK = {"host": HOST, "path": PATH, "online": "boolean",
                "pid_running": "boolean", "aliases": ("list", HOST)}
HOST_FACT = {"host": HOST, "short_hostname": HOST, "fqdn": HOST,
             "ips": ("list", HOST), "aliases": ("list", HOST)}
SHD = {"host": HOST, "online": "boolean", "pid_running": "boolean",
       "source": frozenset({"text", "xml"})}
HEAL_SETTINGS = {
    "effective_settings": {
        "cluster.self-heal-daemon": frozenset({"on", "off", "(default)"}),
        "cluster.data-self-heal": frozenset({"on", "off", "(default)"}),
        "cluster.metadata-self-heal": frozenset({"on", "off", "(default)"}),
        "cluster.entry-self-heal": frozenset({"on", "off", "(default)"}),
    },
    "all_on": "boolean", "all_off": "boolean",
}
HEALTH = {
    "volume": ("alias", "volume"), "available": "boolean",
    "volume_type": VOLUME_TYPES, "hosts": ("list", HOST),
    "host_facts": ("list", HOST_FACT), "bricks": ("list", HEALTH_BRICK),
    "self_heal_daemons": ("list", SHD), "checks": ("list", HEALTH_CHECK),
    "heal_settings": HEAL_SETTINGS,
    "reversibility": {"snapshot_required": "boolean", "snapshot_acknowledged": "boolean",
                        "snapshot_inventory": {"available": "boolean", "count": "integer"}},
    "summary": {key: rule for key, rule in {
        "checks_checked": "integer", "checks_ok": "integer", "checks_failed": "integer",
        "hosts_checked": "integer", "hosts_ok": "integer", "hosts_failed": "integer",
        "bricks_checked": "integer", "bricks_online": "integer",
        "bricks_offline": "integer", "ready": "boolean",
    }.items()},
}

OBSERVATION = {
    "host": HOST, "raw_entry": PATH, "gfid": "gfid", "file_gfid": "gfid",
    "backend": PATH, "relpath": PATH, "mounted": PATH, "child_rel": PATH,
    "gfid_path": PATH, "file_gfid_path": PATH, "type": TYPES, "depth": "integer",
    "backend_size": "integer", "backend_mode": "mode", "backend_mode_bits": "mode_bits",
    "backend_uid": ("identity", "uid"), "backend_gid": ("identity", "gid"),
    "backend_child_names": ("list", PATH),
}
for prefix in ("backend", "mounted", "gfid_path", "backend_gfid_path",
               "backend_terminal", "gfid_path_terminal"):
    for suffix in ("exists", "lexists", "checked", "is_symlink"):
        OBSERVATION[f"{prefix}_{suffix}"] = "boolean"
    OBSERVATION[f"{prefix}_lstat_type"] = TYPES
    OBSERVATION[f"{prefix}_trusted_gfid"] = "gfid"
    for suffix in ("path", "readlink"):
        OBSERVATION[f"{prefix}_{suffix}"] = PATH
OBSERVATION["gfid_exists"] = "boolean"

OBJECT = {
    "logical_path": PATH, "object_type": TYPES, "depth": "integer",
    "source_hosts": ("list", HOST), "raw_entries": ("list", PATH),
    "child_names": ("list", PATH), "brick_roles_by_host": ROLE_MAP,
    "brick_host_aliases": HOST_ALIASES,
    "observations": ("map", HOST, ("list", OBSERVATION)),
}
for field in ("gfids", "parent_gfids", "file_gfids", "dead_gfids"):
    OBJECT[field] = ("list", "gfid")

ACTION = {
    "action_id": ACTION_ID, "logical_path": PATH, "action_type": ACTIONS,
    "object_type": TYPES, "depth": "integer", "depends_on": ("list", ACTION_ID),
    "execution_wave": "integer", "parallel_safe": "boolean", "provisional": "boolean",
    "native_heal_first": "boolean", "rescan_after_apply": "boolean",
    "brick_roles_by_host": ROLE_MAP, "brick_host_aliases": HOST_ALIASES,
    "winner_host": HOST, "winner_backend": PATH, "winner_size": "integer",
    "winner_file_gfid": "gfid", "directory_canonical_gfid": "gfid",
    "metadata_source_host": HOST, "metadata_source_backend": PATH,
    "gluster_visible_metadata_split_brain": "boolean",
    "dead_gfids": ("list", "gfid"),
}
for field in ("healthy_hosts", "stale_hosts", "missing_hosts", "conflict_hosts",
              "metadata_majority_hosts", "metadata_mismatch_hosts"):
    ACTION[field] = ("list", HOST)
ACTION["metadata_tuple_by_host"] = ("map", HOST, {
    "mode": "mode", "mode_bits": "mode_bits",
    "uid": ("identity", "uid"), "gid": ("identity", "gid"),
})
STEP = {
    "step_id": ("alias", "step"), "step_type": STEPS,
    "host": HOST, "source_host": HOST, "source_path": PATH, "target_path": PATH,
    "status": STATES, "returncode": "integer", "tolerate_missing": "boolean",
    "via_mount": "boolean",
}
RESULT = {
    **ACTION, "status": STATES, "steps": ("list", STEP), "revert_steps": ("list", STEP),
    "execution_mode": frozenset({"dry-run", "execute"}),
    "backup_mode": frozenset({"required", "best-effort", "none"}),
    "estimated_stage_bytes": "integer", "estimated_backup_bytes": "integer",
    "estimated_unknown_backup_items": "integer",
}
STATUS = {
    "volume": ("alias", "volume"), "write_occurred": "boolean",
    "write_outcome_unknown": "boolean", "execution_write_state": STATES,
    "execution_interrupted": "boolean", "heal_restore_required": "boolean",
    "summary": {key: "integer" for key in (
        "executed_actions", "completed_actions", "failed_actions", "unknown_actions",
        "actions_total", "ready_repairs", "review_items")},
}
SCHEMAS = {
    "manifest": {"objects": ("map", PATH, OBJECT), "origin_binding": BINDING},
    "observations": {"observations": ("list", OBSERVATION)},
    "plan": {"actions": ("list", ACTION), "origin_binding": BINDING},
    "apply": {"actions": ("list", RESULT), "origin_binding": BINDING},
    "execute_results": {"actions": ("list", RESULT), "origin_binding": BINDING},
    "status": STATUS,
    "health": HEALTH,
    "afr_inspection": {"path": PATH, "afr_xattrs": ("list", {
        "name": ("alias", "afr"), "value_hex": "afr", "value_bytes": "afr_size",
    })},
}
REQUIRED = {"manifest": ("objects", dict), "observations": ("observations", list),
            "plan": ("actions", list), "apply": ("actions", list),
            "execute_results": ("actions", list), "health": ("summary", dict),
            "afr_inspection": ("afr_xattrs", list)}
OMIT = object()


class MetadataExporter:
    """One instance per bundle preserves aliases across different artifacts."""

    def __init__(self, *, private_values: list[str] | None = None) -> None:
        self.aliases: dict[str, dict[object, str]] = {}
        self.private_values = private_values or []
        self.omitted = 0

    def alias(self, category: str, value: object) -> str:
        aliases = self.aliases.setdefault(category, {})
        if value not in aliases:
            aliases[value] = f"{category}{len(aliases) + 1}"
        return aliases[value]

    def project(self, value: object, schema: object) -> object:
        # Identity strings become aliases. Any other string retained verbatim
        # must also pass the operator's additional private-identifier exclusions.
        if isinstance(value, str) and not isinstance(schema, tuple) and any(
            private.casefold() in value.casefold() for private in self.private_values
        ):
            self.omitted += 1
            return OMIT
        if isinstance(schema, dict) and isinstance(value, dict):
            result = {}
            self.omitted += len(value.keys() - schema.keys())
            for key, rule in schema.items():
                if key in value:
                    item = self.project(value[key], rule)
                    if item is not OMIT:
                        result[key] = item
            return result
        if isinstance(schema, tuple):
            kind = schema[0]
            if kind == "list" and isinstance(value, list):
                return [item for raw in value if (item := self.project(raw, schema[1])) is not OMIT]
            if kind == "map" and isinstance(value, dict):
                result = {}
                for key, raw in value.items():
                    safe_key = self.project(key, schema[1])
                    item = self.project(raw, schema[2])
                    if safe_key is not OMIT and item is not OMIT:
                        result[safe_key] = item
                return result
            if kind == "alias" and isinstance(value, str):
                return self.alias(schema[1], value) if value else ""
            if kind == "identity" and type(value) is int and 0 <= value < 2**32:
                return self.alias(schema[1], value)
        if isinstance(schema, frozenset) and isinstance(value, str) and value in schema:
            return value
        if schema == "boolean" and type(value) is bool:
            return value
        if schema == "integer" and type(value) is int and -(2**63) <= value < 2**63:
            return value
        if schema == "mode_bits" and type(value) is int and 0 <= value <= 0o177777:
            return value
        if schema == "mode" and isinstance(value, str) and re.fullmatch(
            r"(?:[0-7]{3,6}|[bcdlps?-][r-][w-][xsS-][r-][w-][xsS-][r-][w-][xtT-])", value
        ):
            return value
        if schema == "gfid" and isinstance(value, str) and GFID.fullmatch(value):
            return value.lower()
        if schema == "afr" and isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{24}", value):
            return value.lower()
        if schema == "afr_size" and type(value) is int and value == 12:
            return value
        self.omitted += 1
        return OMIT

    def heal(self, content: str) -> dict:
        bricks = []
        current = None
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            match = re.fullmatch(r"Brick (.+?):(/.*)", line)
            if match:
                current = {"host": self.alias("server", match[1]),
                           "path": self.alias("path", match[2]), "entries": []}
                bricks.append(current)
                continue
            if current is not None:
                if line.startswith("Status: ") and line[8:] in {"Connected", "Disconnected"}:
                    current["connected"] = line[8:] == "Connected"
                    continue
                count = re.fullmatch(r"Number of entries: ([0-9]{1,12})", line)
                if count:
                    current["reported_entries"] = int(count[1])
                    continue
                split = line.endswith(" - Is in split-brain")
                entry = line.removesuffix(" - Is in split-brain")
                gfid = re.fullmatch(r"<gfid:(" + GFID.pattern + r")>(/.*)?", entry)
                if entry.startswith("/") or gfid:
                    row = {"split_brain": split}
                    if gfid:
                        identity = self.project(gfid[1], "gfid")
                        if identity is not OMIT:
                            row["gfid"] = identity
                        if gfid[2]:
                            row["child"] = self.alias("path", gfid[2])
                    else:
                        row["path"] = self.alias("path", entry)
                    current["entries"].append(row)
                    continue
            self.omitted += 1
        return {"bricks": bricks}

    def volume_info(self, content: str) -> dict | None:
        volume = ""
        volume_type = ""
        state = ""
        bricks = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("Volume Name:"):
                volume = line.partition(":")[2].strip()
            elif line.startswith("Type:"):
                volume_type = line.partition(":")[2].strip()
            elif line.startswith("Status:"):
                state = line.partition(":")[2].strip()
            elif match := re.fullmatch(r"Brick\d+:\s*(.+?):(/.*?)(?: \(arbiter\))?", line):
                bricks.append({"host": self.alias("server", match[1]),
                               "path": self.alias("path", match[2]),
                               "role": "arbiter" if line.endswith(" (arbiter)") else "data"})
            elif line:
                self.omitted += 1
        if not volume or not bricks or volume_type not in VOLUME_TYPES or state not in VOLUME_STATES:
            return None
        return {"volume": self.alias("volume", volume), "volume_type": volume_type,
                "state": state, "bricks": bricks}

    def volume_status(self, content: str) -> dict | None:
        # Reuse the health parser so wrapped, real Gluster status rows have one
        # definition. Only the parsed metadata is retained.
        from .health import _parse_self_heal_daemon_status, _parse_volume_status
        bricks = _parse_volume_status(content)
        daemons = _parse_self_heal_daemon_status(content)
        if not bricks and not daemons:
            return None
        return {
            "bricks": [{"host": self.alias("server", row["host"]),
                        "path": self.alias("path", row["path"]),
                        "online": bool(row["online"]), "pid_running": bool(row["pid_running"])}
                       for row in bricks],
            "self_heal_daemons": [{"host": self.alias("server", row["host"]),
                                   "online": bool(row["online"]),
                                   "pid_running": bool(row["pid_running"]),
                                   "source": "text"} for row in daemons],
        }

    def export(self, name: str, content: str) -> dict | None:
        self.omitted = 0
        if name == "heal_info":
            metadata = self.heal(content)
            if not metadata["bricks"]:
                return None
        elif name == "volume_info":
            metadata = self.volume_info(content)
            if metadata is None:
                return None
        elif name == "volume_status":
            metadata = self.volume_status(content)
            if metadata is None:
                return None
        elif name in SCHEMAS:
            try:
                raw = json.loads(content)
            except (ValueError, RecursionError):
                return None
            if not isinstance(raw, dict):
                return None
            required = REQUIRED.get(name)
            if required and not isinstance(raw.get(required[0]), required[1]):
                return None
            # Future writer versions require a deliberate schema review.
            if "schema_version" in raw and (type(raw["schema_version"]) is not int or raw["schema_version"] != 1):
                return None
            metadata = self.project(raw, SCHEMAS[name])
            if not metadata:
                return None
        else:
            return None
        return {"diagnostic_schema_version": 1, "artifact": name,
                "metadata": metadata, "omitted_fields_or_lines": self.omitted}
