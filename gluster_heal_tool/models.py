# SPDX-License-Identifier: GPL-2.0-only
"""Data models for the Gluster repair tool."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class HealEntry:
    raw: str
    source_host: str
    brick: str
    index_on_host: int
    input_source: str = "heal_info"
    split_brain: bool = False

    @property
    def kind(self) -> str:
        if self.raw.startswith("/"):
            return "path"
        if self.raw.startswith("<gfid:") and "/" in self.raw:
            return "gfid_child"
        if self.raw.startswith("<gfid:"):
            return "gfid"
        return "unknown"

    @property
    def parent_gfid(self) -> str | None:
        if not self.raw.startswith("<gfid:"):
            return None
        close = self.raw.find(">")
        if close == -1:
            return None
        return self.raw[6:close]

    @property
    def child_name(self) -> str | None:
        if self.kind != "gfid_child":
            return None
        return self.raw.split(">", 1)[1].lstrip("/") or None


@dataclass
class ResolutionObservation:
    host: str
    raw_entry: str
    gfid: str = ""
    child_rel: str = ""
    gfid_path: str = ""
    backend: str = ""
    type: str = "unknown"
    gfid_exists: bool = False
    file_gfid: str = ""
    file_gfid_path: str = ""
    relpath: str = ""
    mounted: str = ""
    depth: int = 0
    mounted_checked: bool = False
    mounted_lexists: bool = False
    mounted_exists: bool = False
    mounted_lstat_type: str = ""
    mounted_error: str = ""
    backend_lexists: bool = False
    backend_exists: bool = False
    backend_mtime: int | None = None
    backend_size: int | None = None
    backend_mode: str = ""
    backend_mode_bits: int | None = None
    backend_uid: int | None = None
    backend_gid: int | None = None
    backend_acl_access_text: str = ""
    backend_acl_default_text: str = ""
    backend_acl_error: str = ""
    backend_lstat_type: str = ""
    backend_is_symlink: bool = False
    backend_readlink: str = ""
    backend_readlink_chain: list[str] = field(default_factory=list)
    backend_terminal_path: str = ""
    backend_terminal_lstat_type: str = ""
    backend_terminal_exists: bool | None = None
    backend_terminal_is_symlink: bool = False
    backend_terminal_readlink: str = ""
    backend_terminal_trusted_gfid: str = ""
    backend_trusted_gfid: str = ""
    backend_gfid_path: str = ""
    backend_gfid_path_checked: bool = False
    backend_gfid_path_lexists: bool = False
    backend_gfid_path_exists: bool = False
    backend_gfid_path_lstat_type: str = ""
    backend_mdata_hex: str = ""
    backend_child_names: list[str] = field(default_factory=list)
    backend_child_scan_error: str = ""
    gfid_path_lexists: bool = False
    gfid_path_exists: bool = False
    gfid_path_lstat_type: str = ""
    gfid_path_is_symlink: bool = False
    gfid_path_readlink: str = ""
    gfid_path_readlink_chain: list[str] = field(default_factory=list)
    gfid_path_terminal_path: str = ""
    gfid_path_terminal_lstat_type: str = ""
    gfid_path_terminal_is_symlink: bool = False
    gfid_path_terminal_readlink: str = ""
    gfid_path_terminal_trusted_gfid: str = ""
    error: str = ""
    gfid_path_terminal_exists: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ManifestObject:
    logical_path: str
    object_type: str
    depth: int
    input_source: str = "heal_info"
    raw_entries: list[str] = field(default_factory=list)
    source_hosts: list[str] = field(default_factory=list)
    parent_gfids: list[str] = field(default_factory=list)
    file_gfids: list[str] = field(default_factory=list)
    gfids: list[str] = field(default_factory=list)
    child_names: list[str] = field(default_factory=list)
    observations: dict[str, list[ResolutionObservation]] = field(default_factory=dict)
    dead_gfids: list[str] = field(default_factory=list)
    unresolved_entries: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    brick_roles_by_host: dict[str, str] = field(default_factory=dict)
    brick_host_aliases: dict[str, list[str]] = field(default_factory=dict)
    brick_role_evidence_required: bool = False
    brick_role_evidence_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["observations"] = {
            host: [obs.to_dict() for obs in values]
            for host, values in self.observations.items()
        }
        return data


@dataclass
class PlanAction:
    action_id: str
    logical_path: str
    action_type: str
    object_type: str
    depth: int
    graph_markers: list[str] = field(default_factory=list)
    graph_node: str = ""
    matrix_key: str = ""
    decision_class: str = ""
    provisional: bool = False
    terminal_policy: str = ""
    rescan_after_apply: bool = False
    native_heal_first: bool = False
    native_heal_fallback_action: str = ""
    native_heal_reason: str = ""
    recommended_choice: str = ""
    recommended_reason: str = ""
    type_mismatch_loser: str = ""
    followup_edges: list[dict[str, Any]] = field(default_factory=list)
    repair_strategy: str = ""
    depends_on: list[str] = field(default_factory=list)
    execution_wave: int = 0
    parallel_safe: bool = False
    execution_resources: list[str] = field(default_factory=list)
    execution_serialization_reason: str = ""
    winner_host: str = ""
    winner_backend: str = ""
    winner_mtime: int | None = None
    winner_size: int | None = None
    winner_file_gfid: str = ""
    stage_local_path: str = ""
    restore_via_mount: bool = False
    metadata_tuple_by_host: dict[str, dict[str, object]] = field(default_factory=dict)
    metadata_backend_by_host: dict[str, str] = field(default_factory=dict)
    brick_roles_by_host: dict[str, str] = field(default_factory=dict)
    brick_host_aliases: dict[str, list[str]] = field(default_factory=dict)
    brick_role_evidence_required: bool = False
    brick_role_evidence_error: str = ""
    metadata_majority_hosts: list[str] = field(default_factory=list)
    metadata_mismatch_hosts: list[str] = field(default_factory=list)
    metadata_source_host: str = ""
    metadata_source_backend: str = ""
    metadata_source_reason: str = ""
    gluster_visible_metadata_split_brain: bool = False
    metadata_fields_to_align: list[str] = field(default_factory=list)
    stale_hosts: list[str] = field(default_factory=list)
    healthy_hosts: list[str] = field(default_factory=list)
    missing_hosts: list[str] = field(default_factory=list)
    conflict_hosts: list[str] = field(default_factory=list)
    stale_backends: list[str] = field(default_factory=list)
    stale_gfid_paths: list[str] = field(default_factory=list)
    stale_file_gfid_paths: list[str] = field(default_factory=list)
    stale_backends_by_host: dict[str, list[str]] = field(default_factory=dict)
    stale_gfid_paths_by_host: dict[str, list[str]] = field(default_factory=dict)
    stale_file_gfid_paths_by_host: dict[str, list[str]] = field(default_factory=dict)
    dead_gfids: list[str] = field(default_factory=list)
    dead_gfid_live_references: list[str] = field(default_factory=list)
    mounted_target: str = ""
    raw_entries: list[str] = field(default_factory=list)
    file_copies: list[dict[str, Any]] = field(default_factory=list)
    file_cohorts: list[dict[str, Any]] = field(default_factory=list)
    file_metadata_mismatch_hosts: list[str] = field(default_factory=list)
    file_metadata_backend_by_host: dict[str, str] = field(default_factory=dict)
    file_target_backend_by_host: dict[str, str] = field(default_factory=dict)
    arbiter_gfid_repair_hosts: list[str] = field(default_factory=list)
    arbiter_gfid_repair_backend_roots_by_host: dict[str, str] = field(default_factory=dict)
    arbiter_gfid_stale_paths_by_host: dict[str, str] = field(default_factory=dict)
    directory_copies: list[dict[str, Any]] = field(default_factory=list)
    directory_canonical_host: str = ""
    directory_canonical_backend: str = ""
    directory_canonical_gfid: str = ""
    directory_canonical_mdata_hex: str = ""
    directory_metadata_mismatch_hosts: list[str] = field(default_factory=list)
    directory_mdata_mismatch_hosts: list[str] = field(default_factory=list)
    directory_mdata_by_host: dict[str, str] = field(default_factory=dict)
    directory_backend_by_host: dict[str, str] = field(default_factory=dict)
    directory_target_backend_by_host: dict[str, str] = field(default_factory=dict)
    directory_target_backend_root_by_host: dict[str, str] = field(default_factory=dict)
    directory_child_names_by_host: dict[str, list[str]] = field(default_factory=dict)
    missing_directory_children_by_host: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplyStep:
    step_id: str
    step_type: str
    host: str = ""
    source_host: str = ""
    source_path: str = ""
    target_path: str = ""
    tolerate_missing: bool = False
    via_mount: bool = False
    status: str = "planned"
    returncode: int | None = None
    message: str = ""
    command_preview: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BackupArtifact:
    host: str
    source_path: str
    backup_path: str
    kind: str
    estimated_bytes: int | None = None
    required_for_revert: bool = True
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplyActionResult:
    action_id: str
    logical_path: str
    action_type: str
    execution_mode: str
    backup_root: str
    backup_mode: str
    batch: bool
    status: str
    depends_on: list[str] = field(default_factory=list)
    execution_wave: int = 0
    parallel_safe: bool = False
    execution_resources: list[str] = field(default_factory=list)
    execution_serialization_reason: str = ""
    steps: list[ApplyStep] = field(default_factory=list)
    revert_steps: list[ApplyStep] = field(default_factory=list)
    backup_artifacts: list[BackupArtifact] = field(default_factory=list)
    revert_dirs_to_create: list[str] = field(default_factory=list)
    estimated_stage_bytes: int = 0
    estimated_backup_bytes: int = 0
    estimated_unknown_backup_items: int = 0
    native_heal_first: bool = False
    native_heal_fallback_action: str = ""
    native_heal_reason: str = ""
    recommended_choice: str = ""
    recommended_reason: str = ""
    brick_roles_by_host: dict[str, str] = field(default_factory=dict)
    brick_host_aliases: dict[str, list[str]] = field(default_factory=dict)
    brick_role_evidence_required: bool = False
    brick_role_evidence_error: str = ""
    decision: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["steps"] = [step.to_dict() for step in self.steps]
        data["revert_steps"] = [step.to_dict() for step in self.revert_steps]
        data["backup_artifacts"] = [artifact.to_dict() for artifact in self.backup_artifacts]
        return data
