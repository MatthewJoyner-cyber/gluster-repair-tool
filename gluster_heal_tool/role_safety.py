# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Role evidence and payload-source safety checks."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


_VALID_ROLES = {"data", "arbiter"}
_PAYLOAD_STEP_TYPES = {
    "stage_winner_local",
    "restore_file_backend_gap_fill",
    "resolve_split_brain_gluster_cli",
}


def normalize_brick_roles(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(host).strip(): str(role).strip().lower()
        for host, role in value.items()
        if str(host).strip() and str(role).strip()
    }


def normalize_brick_host_aliases(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, list[str]] = {}
    for host, aliases in value.items():
        canonical = str(host).strip()
        if not canonical:
            continue
        items = [canonical]
        if isinstance(aliases, Sequence) and not isinstance(aliases, (str, bytes)):
            items.extend(str(alias).strip() for alias in aliases if str(alias).strip())
        normalized[canonical] = list(dict.fromkeys(items))
    return normalized


def _host_key(value: str) -> str:
    return str(value or "").strip().rstrip(".").lower()


def _canonical_role_host(
    roles: Mapping[str, str],
    host: str,
    brick_host_aliases: Mapping[str, Sequence[str]] | None = None,
) -> str:
    wanted = _host_key(host)
    if not wanted:
        return ""
    for candidate in roles:
        if _host_key(candidate) == wanted:
            return str(candidate).strip()
    alias_map = normalize_brick_host_aliases(brick_host_aliases)
    matches = {
        candidate
        for candidate in roles
        if any(_host_key(alias) == wanted for alias in alias_map.get(str(candidate).strip(), []))
    }
    return next(iter(matches)) if len(matches) == 1 else ""


def role_for_host(
    roles: Mapping[str, str],
    host: str,
    brick_host_aliases: Mapping[str, Sequence[str]] | None = None,
) -> str:
    canonical = _canonical_role_host(roles, host, brick_host_aliases)
    if canonical:
        return str(roles.get(canonical) or "").strip().lower()
    return ""


def role_evidence_error(
    roles: Mapping[str, str] | None,
    *,
    required: bool = False,
    evidence_error: str = "",
    candidate_hosts: Sequence[str] = (),
    brick_host_aliases: Mapping[str, Sequence[str]] | None = None,
) -> str:
    normalized = normalize_brick_roles(roles)
    if evidence_error:
        return f"brick role evidence is unavailable: {evidence_error}"
    if required and not normalized:
        return "brick role evidence is missing; refuse repair-capable planning"
    invalid = sorted(host for host, role in normalized.items() if role not in _VALID_ROLES)
    if invalid:
        return "brick role evidence has invalid roles for: " + ", ".join(invalid)
    if "arbiter" in normalized.values():
        if len(normalized) != 3 or list(normalized.values()).count("arbiter") != 1 or list(normalized.values()).count("data") != 2:
            return "arbiter role evidence is inconsistent; expected exactly two data bricks and one arbiter"
    missing = sorted(
        {
            str(host).strip()
            for host in candidate_hosts
            if str(host).strip()
            and not role_for_host(normalized, str(host), brick_host_aliases)
        }
    )
    if required and missing:
        return "brick role evidence is stale or incomplete for: " + ", ".join(missing)
    return ""


def payload_source_error(
    roles: Mapping[str, str] | None,
    host: str,
    *,
    required: bool = False,
    evidence_error: str = "",
    brick_host_aliases: Mapping[str, Sequence[str]] | None = None,
) -> str:
    evidence_problem = role_evidence_error(
        roles,
        required=required,
        evidence_error=evidence_error,
        candidate_hosts=[host],
        brick_host_aliases=brick_host_aliases,
    )
    if evidence_problem:
        return evidence_problem
    role = role_for_host(normalize_brick_roles(roles), host, brick_host_aliases)
    if role == "arbiter":
        return f"arbiter brick {host} cannot be used as a file-content source"
    return ""


def payload_source_hosts_from_result(result: Any) -> list[str]:
    hosts: list[str] = []
    for host in [getattr(result, "metadata_source_host", "")]:
        if str(host).strip() and str(host).strip() not in hosts:
            hosts.append(str(host).strip())
    for step in getattr(result, "steps", []) or []:
        step_type = str(getattr(step, "step_type", "") or "")
        host = str(getattr(step, "source_host", "") or "").strip()
        if not host and step_type in _PAYLOAD_STEP_TYPES:
            host = str(getattr(step, "host", "") or "").strip()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def is_metadata_only_arbiter_resolution(action: Mapping[str, Any], result: Any) -> bool:
    strategy = str(action.get("repair_strategy") or "")
    if strategy not in {"resolve_posix_metadata_source_brick", "align_posix_metadata_selected_source", "align_posix_metadata_majority"}:
        strategy = " ".join(str(note) for note in getattr(result, "notes", []) or [])
        if not any(marker in strategy for marker in {"resolve_posix_metadata_source_brick", "align_posix_metadata_selected_source", "align_posix_metadata_majority"}):
            return False
    return str(getattr(result, "action_type", "")) == "repair_posix_metadata" and not any(
        str(getattr(step, "step_type", "") or "") in {"stage_winner_local", "restore_via_mount", "restore_via_mount_child_gap", "restore_file_backend_gap_fill"}
        for step in getattr(result, "steps", []) or []
    )


def enforce_apply_role_safety(action: Mapping[str, Any], result: Any) -> None:
    roles = normalize_brick_roles(action.get("brick_roles_by_host"))
    brick_host_aliases = normalize_brick_host_aliases(action.get("brick_host_aliases"))
    required = bool(action.get("brick_role_evidence_required"))
    evidence_error = str(action.get("brick_role_evidence_error") or "")
    source_hosts = payload_source_hosts_from_result(result)
    general_error = role_evidence_error(
        roles,
        required=required and bool(source_hosts or result.status in {"planned", "proposed"}),
        evidence_error=evidence_error,
        candidate_hosts=source_hosts,
        brick_host_aliases=brick_host_aliases,
    )
    if general_error and (result.status in {"planned", "proposed"} or source_hosts):
        result.status = "blocked"
        result.steps = []
        result.revert_steps = []
        result.notes.append(f"arbiter safety gate: {general_error}")
        return
    for host in source_hosts:
        if role_for_host(roles, host, brick_host_aliases) != "arbiter":
            continue
        if is_metadata_only_arbiter_resolution(action, result):
            result.notes.append(f"arbiter safety gate: explicit metadata-only source-brick resolution permitted for {host}")
            continue
        result.status = "blocked"
        result.steps = []
        result.revert_steps = []
        result.notes.append(f"arbiter safety gate: arbiter brick {host} cannot be used as a payload source")
        return
