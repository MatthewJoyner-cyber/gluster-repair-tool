# SPDX-License-Identifier: GPL-2.0-only
"""Controller-local path helpers."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path

WORK_ROOT_ENV = "GLUSTER_REPAIR_WORK_ROOT"
BACKUP_ROOT_ENV = "GLUSTER_REPAIR_BACKUP_DIR"


def default_state_root() -> Path:
    value = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(value) if value and Path(value).is_absolute() else Path.home() / ".local" / "state"
    return base / "gluster-repair"


DEFAULT_WORK_ROOT = default_state_root() / "work"


def default_backup_archive_dir() -> Path:
    value = os.environ.get(BACKUP_ROOT_ENV, "").strip()
    return Path(value).expanduser() if value else default_state_root() / "backups"


def default_work_root() -> Path:
    value = os.environ.get(WORK_ROOT_ENV, "").strip()
    if value:
        return Path(value).expanduser()
    return default_state_root() / "work"


def _safe_name(value: str, fallback: str) -> str:
    cleaned = str(value or "").strip().strip("/")
    return cleaned or fallback


def default_status_file_path() -> Path:
    return default_work_root() / "gluster-repair-status.json"


def default_health_report_path(volume: str) -> Path:
    safe_volume = _safe_name(volume, "volume")
    return default_work_root() / "health-check" / f"{safe_volume}-health-check.json"


def default_brick_layout_path(volume: str) -> Path:
    safe_volume = _safe_name(volume, "volume")
    return default_work_root() / "brick-layout" / f"{safe_volume}-brick-layout.json"


def default_heal_info_root() -> Path:
    return default_work_root() / "heal-info"


def default_heal_snapshot_dir(volume: str) -> Path:
    return default_heal_info_root() / _safe_name(volume, "volume")


def default_temp_mount_root() -> Path:
    return default_work_root() / "gluster-repair"


def default_repair_run_root() -> Path:
    return default_work_root() / "runs"


def default_repair_run_dir(volume: str, run_id: str, started_at: datetime | None = None) -> Path:
    safe_volume = _safe_name(volume, "volume")
    safe_run_id = _safe_name(run_id, "run")
    stamp = (started_at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return default_repair_run_root() / f"{stamp}-{safe_volume}-{safe_run_id}"


def default_stage_local_path(logical_path: str) -> str:
    digest = sha1(logical_path.encode("utf-8")).hexdigest()[:12]
    safe_path = logical_path.strip("/") if logical_path else "root"
    return str(default_work_root() / "gluster-repair-stage" / digest / safe_path)


def default_canary_stage_local_path(scenario: str, file_name: str) -> str:
    safe_scenario = _safe_name(scenario, "scenario")
    safe_file = _safe_name(file_name, "artifact")
    return str(default_work_root() / "repair-canary" / safe_scenario / safe_file)
