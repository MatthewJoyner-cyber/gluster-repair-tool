# SPDX-License-Identifier: GPL-2.0-only
"""Explicit Gluster heal performance inspection and tuning helpers."""
from __future__ import annotations

import re
from typing import Any

from .health import build_volume_health_report
from .volume import (
    get_gluster_version,
    get_heal_statistics,
    get_volume_option,
    reset_volume_option,
    run_full_heal,
    set_volume_option,
)

HEAL_TUNING_OPTIONS: dict[str, dict[str, object]] = {
    "cluster.shd-max-threads": {
        "minimum": 1,
        "maximum": 64,
        "description": "parallel self-heal daemon jobs per local brick",
    },
    "cluster.shd-wait-qlength": {
        "minimum": 1,
        "maximum": 65536,
        "description": "queued self-heal daemon entries per subvolume",
    },
    "cluster.background-self-heal-count": {
        "minimum": 0,
        "maximum": 256,
        "description": "parallel client-side background self-heal jobs",
    },
    "cluster.heal-wait-queue-length": {
        "minimum": 0,
        "maximum": 10000,
        "description": "queued client-side background heal jobs",
    },
}


def _capacity_rows(health_report: dict[str, Any]) -> list[dict[str, object]]:
    rows: dict[tuple[str, str], dict[str, object]] = {}
    for check in health_report.get("checks") or []:
        if not isinstance(check, dict):
            continue
        kind = str(check.get("kind") or "")
        if kind not in {"brick_free_space", "brick_free_inodes"}:
            continue
        host = str(check.get("host") or "")
        path = str(check.get("path") or "")
        if not host or not path:
            continue
        row = rows.setdefault(
            (host, path),
            {"host": host, "path": path, "space_ok": False, "inodes_ok": False},
        )
        payload = check.get("payload") or []
        used_percent = str(payload[4]) if isinstance(payload, list) and len(payload) > 4 else ""
        if kind == "brick_free_space":
            row["available_bytes"] = int(check.get("available_bytes") or 0)
            row["space_ok"] = bool(check.get("ok"))
            row["space_used_percent"] = used_percent
        else:
            row["available_inodes"] = int(check.get("available_inodes") or 0)
            row["inodes_ok"] = bool(check.get("ok"))
            row["inodes_used_percent"] = used_percent
    return [rows[key] for key in sorted(rows)]


def _compact_health(health_report: dict[str, Any]) -> dict[str, object]:
    summary = health_report.get("summary")
    heal_settings = health_report.get("heal_settings")
    return {
        "available": bool(health_report.get("available")),
        "error": str(health_report.get("error") or ""),
        "checked_at": str(health_report.get("checked_at") or ""),
        "summary": dict(summary) if isinstance(summary, dict) else {},
        "heal_settings": dict(heal_settings) if isinstance(heal_settings, dict) else {},
    }


def _build_capability_report(volume: str) -> tuple[dict[str, object], list[str]]:
    warnings: list[str] = []
    version = ""
    try:
        version = get_gluster_version()
    except RuntimeError as exc:
        warnings.append(f"could not determine controller Gluster version: {exc}")

    version_supported = bool(re.match(r"^\d+(?:\.\d+)+$", version))
    capabilities: dict[str, object] = {
        "controller_gluster": {
            "detected_version": version,
            "available": version_supported,
            "source": "gluster --version",
        },
        "pending_index_heal": {
            "available": version_supported,
            "command_preview": ["gluster", "volume", "heal", volume],
            "scope": "Gluster pending index entries only; this is not full namespace healing",
        },
        "full_namespace_heal": {
            "available": version_supported,
            "command_preview": ["gluster", "volume", "heal", volume, "full"],
            "scope": "entire namespace; explicit --full-heal-once and --execute -y only",
        },
        "per_file_split_brain_resolution": {
            "available": version_supported,
            "scope": "only evidence-bound split-brain policy commands; use the repair planner",
            "command_shape": [
                "gluster",
                "volume",
                "heal",
                volume,
                "split-brain",
                "source-brick|latest-mtime|bigger-file",
                "<path-or-gfid>",
            ],
        },
    }
    try:
        granular = get_volume_option(volume, "cluster.granular-entry-heal")
        capabilities["granular_entry_heal"] = {
            "available": True,
            "current": granular,
            "scope": "entry-heal indexing mode, not a path-scoped heal command",
        }
    except RuntimeError as exc:
        capabilities["granular_entry_heal"] = {
            "available": False,
            "current": "",
            "scope": "entry-heal indexing mode, not a path-scoped heal command",
            "reason": str(exc),
        }
        warnings.append("granular-entry-heal state was not available on this controller or volume")
    try:
        statistics = get_heal_statistics(volume)
        capabilities["pending_backlog_statistics"] = {
            "available": True,
            "command_preview": ["gluster", "volume", "heal", volume, "statistics", "heal-count"],
            "output": statistics,
        }
    except RuntimeError as exc:
        capabilities["pending_backlog_statistics"] = {
            "available": False,
            "command_preview": ["gluster", "volume", "heal", volume, "statistics", "heal-count"],
            "reason": str(exc),
        }
        warnings.append("pending heal-count statistics are not available from this controller or volume")
    return capabilities, warnings


def build_heal_performance_report(
    volume: str,
    *,
    ssh_user: str,
    connect_timeout: float,
    health_report: dict[str, Any] | None = None,
) -> dict[str, object]:
    health = health_report or build_volume_health_report(
        volume,
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
    )
    capabilities, capability_warnings = _build_capability_report(volume)
    options: list[dict[str, object]] = []
    warnings = list(capability_warnings)
    errors: list[str] = []
    for option, spec in HEAL_TUNING_OPTIONS.items():
        try:
            current = get_volume_option(volume, option)
        except RuntimeError as exc:
            current = ""
            warnings.append(str(exc))
        options.append(
            {
                "option": option,
                "current": current,
                "minimum": spec["minimum"],
                "maximum": spec["maximum"],
                "description": spec["description"],
            }
        )

    capacity = _capacity_rows(health)
    summary = health.get("summary") or {}
    if not summary.get("ready"):
        warnings.append("health is not ready; do not raise heal concurrency")
    for row in capacity:
        if not row.get("space_ok") or not row.get("inodes_ok"):
            warnings.append(
                "capacity evidence is incomplete for {}:{}".format(
                    row["host"], row["path"]
                )
            )
        if row.get("available_bytes", 1) <= 0 or row.get("available_inodes", 1) <= 0:
            warnings.append(
                "brick {}:{} has no reported free blocks or inodes".format(
                    row["host"], row["path"]
                )
            )

    compact_health = _compact_health(health)
    return {
        "schema_version": 1,
        "volume": volume,
        "health": compact_health,
        "capacity": capacity,
        "options": options,
        "capabilities": capabilities,
        "warnings": warnings,
        "errors": errors,
        "full_heal_policy": {
            "default": "never",
            "note": "full namespace healing is never automatic; normal pending/index healing is separate",
        },
    }


def validate_tuning_request(requested: dict[str, int | None]) -> tuple[dict[str, str], list[str]]:
    changes: dict[str, str] = {}
    errors: list[str] = []
    for option, value in requested.items():
        if value is None:
            continue
        spec = HEAL_TUNING_OPTIONS[option]
        minimum = int(spec["minimum"])
        maximum = int(spec["maximum"])
        if value < minimum or value > maximum:
            errors.append(f"{option} must be between {minimum} and {maximum}, got {value}")
            continue
        changes[option] = str(value)
    return changes, errors


def _is_default_setting(value: str) -> bool:
    return value.strip().upper() == "(DEFAULT)"


def _rollback_action(volume: str, option: str, value: str) -> dict[str, object]:
    if _is_default_setting(value):
        return {
            "operation": "reset",
            "command_preview": ["gluster", "volume", "reset", volume, option],
        }
    return {
        "operation": "set",
        "command_preview": ["gluster", "volume", "set", volume, option, value],
    }


def build_tuning_preview(
    report: dict[str, object],
    requested: dict[str, int | None],
) -> dict[str, object]:
    changes, errors = validate_tuning_request(requested)
    current = {
        str(item.get("option") or ""): str(item.get("current") or "")
        for item in report.get("options") or []
        if isinstance(item, dict)
    }
    rollback = {
        option: current.get(option, "")
        for option in changes
        if current.get(option, "")
    }
    missing_rollback = [option for option in changes if option not in rollback]
    if missing_rollback:
        errors.append("cannot tune without a current rollback value for: " + ", ".join(missing_rollback))
    volume = str(report.get("volume") or "<volume>")
    rollback_actions = {
        option: _rollback_action(volume, option, value)
        for option, value in rollback.items()
    }
    warnings = list(report.get("warnings") or [])
    health = report.get("health") or {}
    summary = health.get("summary") if isinstance(health, dict) else {}
    if changes and (not isinstance(summary, dict) or not summary.get("ready")):
        errors.append("health is not ready; tuning apply is blocked")
    return {
        "changes": changes,
        "rollback": rollback,
        "rollback_actions": rollback_actions,
        "errors": errors,
        "warnings": warnings,
        "ready_to_apply": bool(changes) and not errors,
    }


def apply_tuning(preview: dict[str, object], volume: str) -> dict[str, object]:
    changes = dict(preview.get("changes") or {})
    rollback = dict(preview.get("rollback") or {})
    rollback_actions = dict(preview.get("rollback_actions") or {})
    errors = list(preview.get("errors") or [])
    attempted: list[str] = []
    applied: list[str] = []
    rolled_back: list[str] = []
    if errors:
        return {
            "attempted": attempted,
            "applied": applied,
            "rolled_back": rolled_back,
            "rollback": rollback,
            "rollback_actions": rollback_actions,
            "errors": errors,
        }
    try:
        for option, value in changes.items():
            set_volume_option(volume, str(option), str(value))
            attempted.append(str(option))
            applied.append(str(option))
    except RuntimeError as exc:
        errors.append(str(exc))
        for option in reversed(applied):
            previous = str(rollback[option])
            try:
                if _is_default_setting(previous):
                    reset_volume_option(volume, option)
                else:
                    set_volume_option(volume, option, previous)
                rolled_back.append(option)
                applied.remove(option)
            except RuntimeError as rollback_exc:
                errors.append(f"rollback failed for {option}: {rollback_exc}")
    return {
        "attempted": attempted,
        "applied": applied,
        "rolled_back": rolled_back,
        "rollback": rollback,
        "rollback_actions": rollback_actions,
        "errors": errors,
    }


def execute_heal_performance(
    volume: str,
    *,
    ssh_user: str,
    connect_timeout: float,
    requested: dict[str, int | None],
    full_heal_mode: str,
    execute: bool,
    batch: bool,
) -> dict[str, object]:
    report = build_heal_performance_report(
        volume,
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
    )
    preview = build_tuning_preview(report, requested)
    full_heal = {
        "mode": full_heal_mode,
        "launched": False,
        "command_preview": ["gluster", "volume", "heal", volume, "full"]
        if full_heal_mode == "once"
        else [],
    }
    errors = list(report.get("errors") or []) + list(preview.get("errors") or [])
    if full_heal_mode not in {"never", "already-run", "once"}:
        errors.append(f"unsupported full-heal mode: {full_heal_mode}")
    full_launch_requested = full_heal_mode == "once"
    changes_requested = bool(preview.get("changes"))
    if full_launch_requested:
        capabilities = report.get("capabilities") or {}
        full_capability = (
            capabilities.get("full_namespace_heal", {})
            if isinstance(capabilities, dict)
            else {}
        )
        if not isinstance(full_capability, dict) or not full_capability.get("available"):
            errors.append("full heal is blocked until the controller Gluster version is identified")
    if execute and (changes_requested or full_launch_requested) and not batch:
        errors.append("heal-performance writes require --execute -y")
    if execute and not errors:
        if changes_requested:
            tuning_result = apply_tuning(preview, volume)
            preview["apply"] = tuning_result
            errors.extend(tuning_result.get("errors") or [])
        if full_launch_requested and not errors:
            health = report.get("health") or {}
            summary = health.get("summary") if isinstance(health, dict) else {}
            if not isinstance(summary, dict) or not summary.get("ready"):
                errors.append("health is not ready; full heal launch is blocked")
            else:
                try:
                    run_full_heal(volume)
                except RuntimeError as exc:
                    errors.append(f"full heal launch failed: {exc}")
                else:
                    full_heal["launched"] = True
    report["tuning_preview"] = preview
    report["full_heal"] = full_heal
    report["errors"] = errors
    return report
