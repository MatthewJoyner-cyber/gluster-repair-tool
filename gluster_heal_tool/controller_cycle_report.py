# SPDX-License-Identifier: GPL-2.0-only
"""Read-only controller cycle reporting helpers."""
from __future__ import annotations

from typing import Any

from .planner_graph import render_graph_cycle_status_report


def _normalize_guard(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return value


def summarize_controller_cycle(status: dict[str, Any]) -> dict[str, object]:
    graph_cycle = status.get("graph_cycle") or {}
    if not isinstance(graph_cycle, dict):
        graph_cycle = {}
    heal_guard = _normalize_guard(status.get("final_check_guard"))
    graph_cycle_present = bool(graph_cycle)
    graph_cycle_repeat_count = int(graph_cycle.get("repeat_count", 0) or 0)
    graph_cycle_detected = bool(graph_cycle.get("cycle_detected"))
    heal_repeat_count = int(heal_guard.get("repeat_count", 0) or 0)
    heal_hit_limit = bool(heal_guard.get("hit_limit"))
    heal_churn_count = int(heal_guard.get("churn_count", 0) or 0)
    heal_churn_hit_limit = bool(heal_guard.get("churn_hit_limit"))
    stop_reasons: list[str] = []
    if heal_hit_limit:
        stop_reasons.append("heal info repeat limit reached")
    if heal_churn_hit_limit:
        stop_reasons.append("heal info churn limit reached")
    if graph_cycle_detected:
        stop_reasons.append("graph cycle detected")
    stored_cycle = _normalize_guard(status.get("controller_cycle"))
    if stored_cycle:
        graph_cycle_present = bool(stored_cycle.get("graph_cycle_present", graph_cycle_present))
        graph_cycle_repeat_count = int(stored_cycle.get("graph_cycle_repeat_count", graph_cycle_repeat_count) or 0)
        graph_cycle_detected = bool(stored_cycle.get("graph_cycle_detected", graph_cycle_detected))
        heal_repeat_count = int(stored_cycle.get("heal_repeat_count", heal_repeat_count) or 0)
        heal_hit_limit = bool(stored_cycle.get("heal_repeat_hit_limit", heal_hit_limit))
        heal_churn_count = int(stored_cycle.get("heal_churn_count", heal_churn_count) or 0)
        heal_churn_hit_limit = bool(stored_cycle.get("heal_churn_hit_limit", heal_churn_hit_limit))
        controller_reason = str(stored_cycle.get("controller_reason") or "")
        if controller_reason:
            stop_reasons = [controller_reason]
        stored_stop = stored_cycle.get("controller_stop")
        if stored_stop is not None:
            controller_stop = bool(stored_stop)
        else:
            controller_stop = bool(stop_reasons)
    else:
        controller_stop = bool(stop_reasons)
    return {
        "controller_stop": controller_stop,
        "controller_reason": "; ".join(stop_reasons) if stop_reasons else "controller can continue",
        "graph_cycle_present": graph_cycle_present,
        "graph_cycle_repeat_count": graph_cycle_repeat_count,
        "graph_cycle_detected": graph_cycle_detected,
        "graph_cycle_report": render_graph_cycle_status_report(status),
        "heal_repeat_count": heal_repeat_count,
        "heal_repeat_hit_limit": heal_hit_limit,
        "heal_churn_count": heal_churn_count,
        "heal_churn_hit_limit": heal_churn_hit_limit,
        "heal_repeat_warning": str(heal_guard.get("warning") or ""),
    }


def controller_cycle_next_action(summary: dict[str, object]) -> str:
    graph_cycle_detected = bool(summary.get("graph_cycle_detected"))
    heal_repeat_hit_limit = bool(summary.get("heal_repeat_hit_limit"))
    heal_churn_hit_limit = bool(summary.get("heal_churn_hit_limit"))
    if graph_cycle_detected and heal_repeat_hit_limit:
        return "rebuild-plan"
    if graph_cycle_detected:
        return "review-graph-cycle"
    if heal_repeat_hit_limit:
        return "investigate-repair-gap"
    if heal_churn_hit_limit:
        return "review-heal-churn"
    if bool(summary.get("controller_stop")):
        return "rebuild-plan"
    return "continue"


def build_controller_cycle_status(status: dict[str, Any]) -> dict[str, object]:
    summary = summarize_controller_cycle(status)
    return {
        "controller_cycle": {
            "controller_stop": summary["controller_stop"],
            "controller_reason": summary["controller_reason"],
            "controller_next_action": controller_cycle_next_action(summary),
            "graph_cycle_present": summary["graph_cycle_present"],
            "graph_cycle_repeat_count": summary["graph_cycle_repeat_count"],
            "graph_cycle_detected": summary["graph_cycle_detected"],
            "heal_repeat_count": summary["heal_repeat_count"],
            "heal_repeat_hit_limit": summary["heal_repeat_hit_limit"],
            "heal_churn_count": summary["heal_churn_count"],
            "heal_churn_hit_limit": summary["heal_churn_hit_limit"],
            "heal_repeat_warning": summary["heal_repeat_warning"],
        },
        "controller_cycle_report": render_controller_cycle_report(status),
    }


def controller_cycle_gate_warnings(status: dict[str, Any]) -> list[str]:
    summary = summarize_controller_cycle(status)
    if summary["controller_stop"]:
        return [f"controller-cycle gate is stopping execution: {summary['controller_reason']}"]
    return []


def render_controller_cycle_report(status: dict[str, Any]) -> str:
    stored_report = str(status.get("controller_cycle_report") or "").strip()
    if stored_report:
        return stored_report
    summary = summarize_controller_cycle(status)
    next_action = controller_cycle_next_action(summary)
    stop = "yes" if summary["controller_stop"] else "no"
    graph_present = "yes" if summary["graph_cycle_present"] else "no"
    graph_detected = str(bool(summary["graph_cycle_detected"])).lower()
    heal_hit_limit = str(bool(summary["heal_repeat_hit_limit"])).lower()
    heal_churn_hit_limit = str(bool(summary["heal_churn_hit_limit"])).lower()
    lines = [
        f"controller-cycle: stop={stop}, next_action={next_action}, reason={summary['controller_reason']}",
        (
            "controller-cycle-graph: present="
            f"{graph_present}, repeat_count={summary['graph_cycle_repeat_count']}, cycle_detected={graph_detected}"
        ),
        summary["graph_cycle_report"],
        (
            "controller-cycle-heal: repeat_count="
            f"{summary['heal_repeat_count']}, churn_count={summary['heal_churn_count']}, "
            f"hit_limit={heal_hit_limit}, churn_hit_limit={heal_churn_hit_limit}"
            + (f", warning={summary['heal_repeat_warning']}" if summary["heal_repeat_warning"] else "")
        ),
    ]
    return "\n".join(lines)
