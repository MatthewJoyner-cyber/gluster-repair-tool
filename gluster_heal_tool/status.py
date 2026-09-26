# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Status tracking helpers."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .controller_paths import default_status_file_path
from .shared_io import shared_exclusive_lock, write_json_shared


DEFAULT_STATUS_FILE = str(default_status_file_path())
HEALTH_REPORT_STALE_SECONDS = 600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_status(path: str | Path) -> dict[str, Any]:
    status_path = Path(path)
    if not status_path.exists():
        return {}
    return json.loads(status_path.read_text())


def merge_write_history(*records: object) -> dict[str, Any]:
    """Return the irreversible write facts recorded by status-like artifacts.

    A later read-only refresh cannot erase a completed write, and an interrupted
    or reportless execution remains explicitly unknown until an operator has
    inspected the execution artifacts.
    """
    wrote = False
    unknown = False
    write_state = ""
    for record in records:
        if not isinstance(record, dict):
            continue
        wrote = wrote or bool(record.get("write_occurred"))
        unknown = unknown or bool(record.get("write_outcome_unknown"))
        state = str(record.get("execution_write_state") or "").strip()
        unknown = unknown or state == "unknown" or bool(
            record.get("execution_interrupted") or record.get("interrupted")
        )
        if state and state != "unknown":
            write_state = state
        for summary_key in ("execution_summary", "summary"):
            summary = record.get(summary_key)
            if not isinstance(summary, dict):
                continue
            executed = int(summary.get("executed_actions", 0) or 0)
            completed = int(summary.get("completed_actions", 0) or 0)
            failed = int(summary.get("failed_actions", 0) or 0)
            wrote = wrote or bool(executed or completed)
            unknown = unknown or bool(summary.get("unknown_actions")) or bool(failed and (executed or completed))
    if unknown:
        write_state = "unknown"
    elif wrote and not write_state:
        write_state = "completed"
    return {
        "write_occurred": wrote,
        "write_outcome_unknown": unknown,
        "execution_write_state": write_state,
    }


def apply_write_history(target: dict[str, Any], *records: object) -> dict[str, Any]:
    """Update one artifact with write facts merged from every related artifact."""
    history = merge_write_history(target, *records)
    target.update(history)
    return history


def update_status(path: str | Path, **fields: Any) -> dict[str, Any]:
    with shared_exclusive_lock(path):
        current = load_status(path)
        if "created_at" not in current:
            current["created_at"] = _now_iso()
        fields.update(merge_write_history(current, fields, fields.get("summary")))
        current.update(fields)
        current["updated_at"] = _now_iso()
        write_json_shared(path, current)
        return current


def health_check_warnings(status: dict[str, Any], *, max_age_seconds: int = HEALTH_REPORT_STALE_SECONDS) -> list[str]:
    from .health import health_check_warnings as _health_check_warnings

    return _health_check_warnings(status, max_age_seconds=max_age_seconds)


def snapshot_gate_warnings(
    status: dict[str, Any],
    *,
    require_snapshot: bool = False,
    snapshot_ack: bool = False,
) -> list[str]:
    warnings: list[str] = []
    reversibility = status.get("reversibility") or {}
    if not isinstance(reversibility, dict):
        reversibility = {}
    required = bool(require_snapshot or reversibility.get("snapshot_required"))
    acknowledged = bool(snapshot_ack or reversibility.get("snapshot_acknowledged"))
    if required and not acknowledged:
        warnings.append(
            "snapshot confirmation is required before execute; take a Gluster snapshot or equivalent rollback point, then pass --snapshot-ack"
        )
    return warnings


def render_reversibility_summary(status: dict[str, Any]) -> str:
    reversibility = status.get("reversibility") or {}
    if not isinstance(reversibility, dict) or not reversibility:
        return ""
    parts: list[str] = []
    if reversibility.get("snapshot_required"):
        parts.append("snapshot gate required")
    else:
        parts.append("snapshot gate optional")
    if reversibility.get("snapshot_acknowledged"):
        parts.append("snapshot acknowledged")
    inventory = reversibility.get("snapshot_inventory") or {}
    if isinstance(inventory, dict):
        if inventory.get("available"):
            count = int(inventory.get("count", 0) or 0)
            parts.append(f"snapshot inventory {count}")
        else:
            error = str(inventory.get("error") or "").strip()
            parts.append("snapshot inventory unavailable" if not error else f"snapshot inventory unavailable: {error}")
    return "; ".join(parts)


def status_warnings(path: str | Path) -> list[str]:
    current = load_status(path)
    warnings: list[str] = []
    heal = current.get("heal") or {}
    final_check_guard = current.get("final_check_guard") or {}
    health_check = current.get("health_check") or {}
    health_summary = health_check.get("summary") or {}
    if current.get("heal_restore_required"):
        warnings.append("heal-related volume options were turned off for this run and still need to be restored")
    if heal.get("all_off"):
        warnings.append("heal-related volume options are currently off")
    for message in heal.get("warnings", []):
        if "name-heal cannot be disabled" in message and message not in warnings:
            warnings.append(message)
    if final_check_guard.get("hit_limit"):
        repeat_count = final_check_guard.get("repeat_count", 0)
        repeat_limit = final_check_guard.get("repeat_limit", 0)
        warnings.append(
            "post-execute heal info shape has not changed for "
            f"{repeat_count} consecutive checks; probable repair gap after heal and mount verification; "
            "inspect the Gluster capability report before any full heal; verify affected paths, "
            "then rebuild evidence or prepare a support draft "
            f"(limit {repeat_limit})"
        )
    if final_check_guard.get("churn_hit_limit"):
        churn_count = final_check_guard.get("churn_count", 0)
        churn_limit = final_check_guard.get("churn_limit", 0)
        warnings.append(
            "post-execute heal info shape changed too many times; probable repair churn after repeated rescan and verify attempts; "
            "inspect the Gluster capability report before any full heal; verify affected paths, "
            "then rebuild evidence or prepare a support draft "
            f"(limit {churn_limit}, count {churn_count})"
        )
    for message in health_summary.get("warnings", []):
        if message not in warnings:
            warnings.append(str(message))
    for message in snapshot_gate_warnings(current):
        if message not in warnings:
            warnings.append(message)
    for message in health_check_warnings(current):
        if message not in warnings:
            warnings.append(message)
    return warnings
