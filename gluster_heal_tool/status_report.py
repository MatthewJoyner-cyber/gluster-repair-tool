# SPDX-License-Identifier: GPL-2.0-only
"""Human-readable controller status reporting."""
from __future__ import annotations

from typing import Any

from .controller_cycle_report import render_controller_cycle_report
from .planner_graph import render_graph_cycle_status_report, render_graph_cycle_status_summary
from .status import render_reversibility_summary


def render_status_report(status: dict[str, Any]) -> str:
    parts: list[str] = []
    phase = str(status.get("phase") or "").strip()
    volume = str(status.get("volume") or "").strip()
    mountpoint = str(status.get("mountpoint") or "").strip()
    if phase or volume or mountpoint:
        parts.append(
            "status: "
            + ", ".join(
                [
                    f"phase={phase or 'unknown'}",
                    f"volume={volume or 'n/a'}",
                    f"mountpoint={mountpoint or 'n/a'}",
                ]
            )
        )
    if status.get("evidence_log"):
        parts.append(f"evidence log: {status['evidence_log']}")
    reversibility = render_reversibility_summary(status)
    if reversibility:
        parts.append(f"reversibility: {reversibility}")
    if status.get("summary"):
        parts.append(f"summary: {status.get('summary')}")
    graph_summary = render_graph_cycle_status_summary(status)
    graph_report = render_graph_cycle_status_report(status)
    if graph_summary != "graph-cycle-status: present=no" or graph_report != "graph-cycle: unavailable":
        parts.append(graph_summary)
        parts.append(graph_report)
    if status.get("controller_cycle") or status.get("controller_cycle_report"):
        parts.append(render_controller_cycle_report(status))
    return "\n".join(parts) if parts else "status: empty"
