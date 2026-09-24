# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Read-only audit helpers for the planner graph."""
from __future__ import annotations

from collections import Counter
from .planner_graph import node_for_action
from .models import PlanAction


_AUTO_MARKER_PREFIXES = (
    "action_type:",
    "repair_strategy:",
    "object_type:",
    "graph_node:",
)


def _structured_action_markers(action: PlanAction) -> set[str]:
    return {
        marker
        for marker in action.graph_markers
        if marker and not marker.startswith(_AUTO_MARKER_PREFIXES)
    }


def _node_structured_markers(node_markers: frozenset[str]) -> set[str]:
    return {
        marker
        for marker in node_markers
        if marker and not marker.startswith(_AUTO_MARKER_PREFIXES)
    }


def summarize_graph_audit(actions: list[PlanAction]) -> dict[str, object]:
    structured_nodes: Counter[str] = Counter()
    note_only_nodes: Counter[str] = Counter()
    unmatched_nodes: Counter[str] = Counter()
    structured = 0
    note_only = 0
    unmatched = 0

    for action in actions:
        node = node_for_action(action)
        if node is None:
            unmatched += 1
            unmatched_nodes[action.logical_path or action.action_id] += 1
            continue
        structured_markers = _structured_action_markers(action)
        node_markers = _node_structured_markers(node.graph_markers)
        if structured_markers and node_markers and structured_markers.intersection(node_markers):
            structured += 1
            structured_nodes[node.node_id] += 1
        else:
            note_only += 1
            note_only_nodes[node.node_id] += 1

    return {
        "actions": len(actions),
        "structured": structured,
        "note_only": note_only,
        "unmatched": unmatched,
        "structured_nodes": structured_nodes,
        "note_only_nodes": note_only_nodes,
        "unmatched_nodes": unmatched_nodes,
    }


def render_graph_audit_report(actions: list[PlanAction]) -> str:
    summary = summarize_graph_audit(actions)
    header = (
        f"graph-audit: actions={summary['actions']}, "
        f"structured={summary['structured']}, "
        f"note_only={summary['note_only']}, "
        f"unmatched={summary['unmatched']}"
    )
    lines = [header]
    structured_nodes = summary["structured_nodes"]
    note_only_nodes = summary["note_only_nodes"]
    unmatched_nodes = summary["unmatched_nodes"]
    if structured_nodes:
        nodes = ",".join(f"{node}={count}" for node, count in sorted(structured_nodes.items()))
        lines.append(f"graph-audit-structured: nodes={nodes}")
    if note_only_nodes:
        nodes = ",".join(f"{node}={count}" for node, count in sorted(note_only_nodes.items()))
        lines.append(f"graph-audit-note-only: nodes={nodes}")
    if unmatched_nodes:
        nodes = ",".join(f"{path}={count}" for path, count in sorted(unmatched_nodes.items()))
        lines.append(f"graph-audit-unmatched: nodes={nodes}")
    return "\n".join(lines)
