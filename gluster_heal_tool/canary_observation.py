"""Observation helpers for gtest canaries."""
from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any

from .canary_shared import _state_path
from .controller_paths import default_heal_info_root
from .shared_io import write_text_shared


def _observation_log_path(volume: str, scenario: str):
    return _state_path(volume, scenario).with_suffix(".log")


def _latest_heal_snapshot_path(volume: str, heal_root: str | None = None) -> str:
    root = Path(heal_root).expanduser() if heal_root else default_heal_info_root()
    safe_volume = volume.strip().strip("/") or "volume"
    snapshot_dir = root / safe_volume
    if not snapshot_dir.exists():
        return ""
    candidates = sorted(
        snapshot_dir.glob(f"{safe_volume}-heal-info-*.txt"),
        key=lambda path: (
            path.stat().st_mtime_ns if path.exists() else 0,
            path.name,
        ),
    )
    return str(candidates[-1]) if candidates else ""


def _capture_mount_stat_snapshot(path: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "sudo",
            "-n",
            "stat",
            "-c",
            "%n|%F|%i|%h|%a|%u|%g|%s|%Y|%Z",
            path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"path": path, "ok": False, "error": proc.stderr.strip() or proc.stdout.strip() or "stat failed"}
    line = (proc.stdout or "").strip()
    fields = line.split("|")
    if len(fields) != 10:
        return {"path": path, "ok": False, "error": f"unexpected stat output: {line}"}
    return {
        "path": fields[0],
        "kind": fields[1],
        "inode": fields[2],
        "links": fields[3],
        "mode": fields[4],
        "uid": fields[5],
        "gid": fields[6],
        "size": fields[7],
        "mtime": fields[8],
        "ctime": fields[9],
        "ok": True,
    }


def _capture_parent_lookup_snapshot(path: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "sudo",
            "-n",
            "ls",
            "-la",
            "--",
            path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "path": path,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }


def _render_observation_log(state: dict[str, Any], observation: dict[str, Any] | None = None) -> str:
    snapshot_paths = [str(item) for item in state.get("snapshot_paths", []) if str(item)]
    lines = [
        f"volume: {state.get('volume', '')}",
        f"scenario: {state.get('scenario', '')}",
        f"kind: {state.get('kind', '')}",
        f"logical path: {state.get('mount_target', '') or state.get('mount_path', '') or state.get('mount_parent', '')}",
        f"child path: {state.get('mount_child', '') or state.get('missing_child', '') or '(none)'}",
        f"latest heal snapshot: {state.get('latest_heal_snapshot', '') or '(none recorded)'}",
        "",
        "baseline snapshot:",
    ]
    for item in state.get("mount_snapshot", {}).get("paths", []):
        lines.append(f"  - {item.get('path', '')}: {item}")
    baseline_parent_lookup = state.get("baseline_parent_lookup") or {}
    if baseline_parent_lookup:
        lines.extend(
            [
                "",
                "baseline parent lookup:",
                f"  - {baseline_parent_lookup}",
            ]
        )
    if snapshot_paths:
        lines.append(f"  snapshot paths: {', '.join(snapshot_paths)}")
    backend_observations = state.get("backend_observations") or []
    if backend_observations:
        lines.append("")
        lines.append("baseline backend observations:")
        for host_entry in backend_observations:
            lines.append(f"  host {host_entry.get('host', '')}:")
            for result in host_entry.get("results", []):
                lines.append(f"    - {result}")
    if observation is not None:
        lines.extend(
            [
                "",
                f"latest heal snapshot: {observation.get('latest_heal_snapshot', '') or '(none recorded)'}",
                "",
                "current snapshot:",
            ]
        )
        for item in observation.get("paths", []):
            lines.append(f"  - {item.get('path', '')}: {item}")
        parent_lookup = observation.get("parent_lookup") or {}
        if parent_lookup:
            lines.extend(
                [
                    "",
                    "current parent lookup:",
                    f"  - {parent_lookup}",
                ]
            )
        brick_observations = observation.get("brick_observations") or []
        if brick_observations:
            lines.append("")
            lines.append("brick observations:")
            for host_entry in brick_observations:
                lines.append(f"  host {host_entry.get('host', '')}:")
                for result in host_entry.get("results", []):
                    lines.append(f"    - {result}")
        backend_observations = observation.get("backend_observations") or []
        if backend_observations:
            lines.append("")
            lines.append("backend observations:")
            for host_entry in backend_observations:
                lines.append(f"  host {host_entry.get('host', '')}:")
                for result in host_entry.get("results", []):
                    lines.append(f"    - {result}")
        lines.extend(
            [
                "",
                f"changed: {observation.get('changed', False)}",
            ]
        )
    return "\n".join(lines) + "\n"


def _write_observation_log(volume: str, scenario: str, state: dict[str, Any], observation: dict[str, Any] | None = None) -> None:
    write_text_shared(_observation_log_path(volume, scenario), _render_observation_log(state, observation))
