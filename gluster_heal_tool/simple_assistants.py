# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Bounded, read-only evidence assistants for simple repair mode."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .decisions import build_split_brain_file_checksum_report
from .directory_tie import build_directory_tie_report
from .role_safety import role_for_host


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _actions(plan: dict[str, object]) -> list[dict[str, object]]:
    return [
        dict(action)
        for action in plan.get("actions", [])
        if isinstance(action, dict) and str(action.get("action_id") or "")
    ]


def _is_ambiguous_file(action: dict[str, object]) -> bool:
    return (
        str(action.get("repair_strategy") or "") == "ambiguous_entry_split_brain_file"
        or (
            str(action.get("action_type") or "") == "review_entry_split_brain"
            and len(action.get("file_cohorts") or []) > 1
        )
    )


def _is_directory(action: dict[str, object]) -> bool:
    return str(action.get("action_type") or "") in {
        "review_directory_children",
        "review_directory_gfid_conflict",
        "review_directory_metadata",
        "repair_directory_metadata",
        "reconcile_directory",
    }


def _is_posix(action: dict[str, object]) -> bool:
    return bool(action.get("metadata_tuple_by_host")) or str(action.get("action_type") or "") in {
        "review_posix_metadata_no_majority",
        "review_file_metadata",
        "repair_posix_metadata",
        "repair_file_metadata",
    }


def _is_live_reference(action: dict[str, object]) -> bool:
    action_type = str(action.get("action_type") or "")
    strategy = str(action.get("repair_strategy") or "")
    return (
        action_type in {
            "cleanup_stale_glusterfs_index",
            "review_dead_gfid_reference",
            "cleanup_dead_gfid",
            "cleanup_dead_file_refs",
            "review_probable_orphaned_symlink",
        }
        or "gfid" in strategy
        or "orphaned_symlink" in strategy
        or bool(action.get("dead_gfid_live_references"))
    )


def _checksum_lines(report: dict[str, object]) -> list[str]:
    outcome = str(report.get("outcome") or "unavailable")
    lines = [
        f"checksum outcome: {outcome} ({report.get('reason') or 'no reason recorded'})",
    ]
    for item in report.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or "unknown-host")
        checksum = str(item.get("sha256") or "unavailable")
        error = str(item.get("error") or "")
        suffix = f"; error={error}" if error else ""
        lines.append(f"checksum {host}: {checksum}{suffix}")
    return lines


def _directory_lines(item: dict[str, object]) -> list[str]:
    budget = item.get("budget") or {}
    classification = item.get("classification") or {}
    lines = [
        "directory-tie report: "
        f"branch={item.get('branch_type') or 'unknown'}, "
        f"recommended={item.get('recommended_choice') or 'defer'}",
        "directory-tie hosts: "
        f"present={', '.join(classification.get('present_hosts') or []) or 'none'}, "
        f"missing={', '.join(classification.get('missing_hosts') or []) or 'none'}",
        "directory-tie budget: "
        f"depth={budget.get('max_depth', '?')}, "
        f"children={budget.get('max_children_per_dir', '?')}, "
        f"total={budget.get('max_total_nodes', '?')}",
    ]
    if budget.get("over_budget"):
        lines.append(f"directory-tie budget stop: {budget.get('stop_reason') or 'limit reached'}")
    if classification.get("unique_child_names"):
        lines.append(
            "directory-tie child names: "
            + ", ".join(str(name) for name in classification["unique_child_names"])
        )
    return lines


def _posix_evidence(action: dict[str, object]) -> dict[str, object]:
    tuples = action.get("metadata_tuple_by_host") or {}
    roles = action.get("brick_roles_by_host") or {}
    aliases = action.get("brick_host_aliases") or {}
    rows: list[dict[str, object]] = []
    for host, value in sorted(tuples.items()):
        if not isinstance(value, dict):
            continue
        rows.append(
            {
                "host": str(host),
                "role": role_for_host(roles, str(host), aliases) or "unknown",
                "tuple": dict(value),
                "backend": str((action.get("metadata_backend_by_host") or {}).get(host) or ""),
            }
        )
    return {
        "rows": rows,
        "majority_hosts": [str(host) for host in action.get("metadata_majority_hosts") or []],
        "mismatch_hosts": [str(host) for host in action.get("metadata_mismatch_hosts") or []],
        "source_host": str(action.get("metadata_source_host") or ""),
        "source_reason": str(action.get("metadata_source_reason") or ""),
        "native_source_required": bool(action.get("gluster_visible_metadata_split_brain")),
        "arbiter_hosts": sorted(
            str(host) for host, role in roles.items() if str(role).lower() == "arbiter"
        ),
    }


def _live_reference_evidence(action: dict[str, object]) -> dict[str, object]:
    live_refs = [str(value) for value in action.get("dead_gfid_live_references") or [] if str(value)]
    stale_by_host = {
        str(host): [str(value) for value in values or [] if str(value)]
        for host, values in (action.get("stale_backends_by_host") or {}).items()
    }
    stale_gfid_by_host = {
        str(host): [str(value) for value in values or [] if str(value)]
        for host, values in (action.get("stale_gfid_paths_by_host") or {}).items()
    }
    terminal_fields: dict[str, object] = {}
    for key, value in action.items():
        if "terminal" in str(key) or "live_reference" in str(key):
            if value not in (None, "", [], {}, False):
                terminal_fields[str(key)] = value
    if live_refs:
        conclusion = "live reference remains; cleanup is not locally safe"
    elif stale_by_host or stale_gfid_by_host or terminal_fields:
        conclusion = "residue proof is recorded, but recheck all source hosts before cleanup"
    else:
        conclusion = "no complete live-reference proof is recorded"
    return {
        "live_references": live_refs,
        "stale_backends_by_host": stale_by_host,
        "stale_gfid_paths_by_host": stale_gfid_by_host,
        "terminal_fields": terminal_fields,
        "conclusion": conclusion,
    }


def _assistant_for_action(
    action: dict[str, object],
    *,
    checksum_report: dict[str, object] | None = None,
    directory_item: dict[str, object] | None = None,
) -> dict[str, object]:
    evidence: list[str] = []
    details: dict[str, object] = {"action": dict(action)}
    if checksum_report is not None:
        details["checksum"] = checksum_report
        evidence.extend(_checksum_lines(checksum_report))
    if directory_item is not None:
        details["directory_tie"] = directory_item
        evidence.extend(_directory_lines(directory_item))
    if _is_posix(action):
        posix = _posix_evidence(action)
        details["posix"] = posix
        if posix["rows"]:
            for row in posix["rows"]:
                evidence.append(
                    "POSIX tuple "
                    f"{row['host']} ({row['role']}): {row['tuple']}"
                )
        else:
            evidence.append("POSIX tuple evidence is unavailable")
        if posix["arbiter_hosts"]:
            evidence.append(
                "arbiter restriction: "
                + ", ".join(posix["arbiter_hosts"])
                + " may provide metadata evidence but never payload source data"
            )
        if posix["native_source_required"]:
            evidence.append("Gluster-visible metadata split-brain requires native source-brick handling")
    if _is_live_reference(action):
        live = _live_reference_evidence(action)
        details["live_reference"] = live
        evidence.append(f"live-reference conclusion: {live['conclusion']}")
        if live["live_references"]:
            evidence.append(
                "live-reference targets: " + ", ".join(live["live_references"])
            )
        if live["terminal_fields"]:
            evidence.append("terminal-path proof fields are present in the plan artifact")

    if not evidence:
        evidence.append("No additional bounded assistant evidence was required for this action.")
    relevant = {
        "action": action,
        "details": details,
        "evidence": evidence,
    }
    return {
        "action_id": str(action.get("action_id") or ""),
        "logical_path": str(action.get("logical_path") or ""),
        "evidence": evidence,
        "details": details,
        "fingerprint": _fingerprint(relevant),
        "topology_fingerprint": _fingerprint(
            {
                "healthy_hosts": action.get("healthy_hosts"),
                "missing_hosts": action.get("missing_hosts"),
                "brick_roles_by_host": action.get("brick_roles_by_host"),
            }
        ),
    }


def build_simple_assistant_report(
    plan: dict[str, object],
    *,
    volume: str,
    worker_path: str,
    ssh_user: str,
    connect_timeout: float = 10.0,
    status: dict[str, object] | None = None,
    checksum_builder: Callable[..., dict[str, object]] | None = None,
    directory_budget: tuple[int, int, int] = (3, 1024, 4096),
) -> dict[str, object]:
    """Build bounded evidence without invoking any repair executor."""
    actions = _actions(plan)
    directory_report: dict[str, object] = {"items": [], "directory_tie_actions": 0}
    if any(_is_directory(action) for action in actions):
        try:
            directory_report = build_directory_tie_report(
                plan,
                max_depth=directory_budget[0],
                max_children_per_dir=directory_budget[1],
                max_total_nodes=directory_budget[2],
            )
        except Exception as exc:  # pragma: no cover - defensive report boundary
            directory_report = {
                "items": [],
                "directory_tie_actions": 0,
                "error": f"directory-tie assistant unavailable: {exc}",
            }
    directory_by_path = {
        str(item.get("logical_path") or ""): item
        for item in directory_report.get("items", []) or []
        if isinstance(item, dict)
    }
    checksum_builder = checksum_builder or build_split_brain_file_checksum_report
    assistant_items: list[dict[str, object]] = []
    checksum_reports: list[dict[str, object]] = []
    for action in actions:
        checksum_report: dict[str, object] | None = None
        if _is_ambiguous_file(action):
            try:
                checksum_report = checksum_builder(
                    action,
                    volume=volume,
                    worker_path=worker_path,
                    ssh_user=ssh_user,
                    connect_timeout=connect_timeout,
                )
            except Exception as exc:  # pragma: no cover - live remote failure path
                checksum_report = {
                    "schema_version": 1,
                    "logical_path": str(action.get("logical_path") or ""),
                    "outcome": "unavailable",
                    "reason": f"checksum assistant unavailable: {exc}",
                    "items": [],
                    "checksums": [],
                    "errors": [str(exc)],
                }
            checksum_reports.append(checksum_report)
        assistant_items.append(
            _assistant_for_action(
                action,
                checksum_report=checksum_report,
                directory_item=directory_by_path.get(str(action.get("logical_path") or "")),
            )
        )
    health = (status or {}).get("health_check") or {}
    health_summary = health.get("summary") if isinstance(health, dict) else {}
    health_warnings = list((health_summary or {}).get("warnings") or []) if isinstance(health_summary, dict) else []
    return {
        "schema_version": 1,
        "mode": "read-only",
        "generated_at": _now_iso(),
        "volume": volume,
        "plan_fingerprint": _fingerprint(plan),
        "topology_fingerprint": _fingerprint(
            [item.get("topology_fingerprint") for item in assistant_items]
        ),
        "health_retry": {
            "ready": bool((health_summary or {}).get("ready")) if isinstance(health_summary, dict) else False,
            "warnings": [str(value) for value in health_warnings],
            "recommended": "retry health and refresh evidence before execution" if health_warnings else "health retry not currently required",
        },
        "directory_tie": directory_report,
        "checksum_reports": checksum_reports,
        "items": assistant_items,
        "summary": {
            "actions": len(actions),
            "assistant_items": len(assistant_items),
            "checksum_reports": len(checksum_reports),
            "directory_tie_actions": int(directory_report.get("directory_tie_actions") or 0),
            "health_warnings": len(health_warnings),
        },
    }
