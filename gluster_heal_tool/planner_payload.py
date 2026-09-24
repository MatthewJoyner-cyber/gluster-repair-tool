# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Helpers for loading planner payloads into model objects."""
from __future__ import annotations

from typing import Any

from .models import PlanAction


def load_plan_actions(payload: dict[str, Any]) -> list[PlanAction]:
    actions: list[PlanAction] = []
    for item in payload.get("actions", []):
        if not isinstance(item, dict):
            continue
        actions.append(
            PlanAction(
                action_id=str(item.get("action_id") or ""),
                logical_path=str(item.get("logical_path") or ""),
                action_type=str(item.get("action_type") or ""),
                object_type=str(item.get("object_type") or ""),
                depth=int(item.get("depth") or 0),
                graph_markers=list(item.get("graph_markers") or []),
                graph_node=str(item.get("graph_node") or ""),
                matrix_key=str(item.get("matrix_key") or ""),
                decision_class=str(item.get("decision_class") or ""),
                provisional=bool(item.get("provisional")),
                terminal_policy=str(item.get("terminal_policy") or ""),
                rescan_after_apply=bool(item.get("rescan_after_apply")),
                native_heal_first=bool(item.get("native_heal_first")),
                native_heal_fallback_action=str(item.get("native_heal_fallback_action") or ""),
                native_heal_reason=str(item.get("native_heal_reason") or ""),
                recommended_choice=str(item.get("recommended_choice") or ""),
                recommended_reason=str(item.get("recommended_reason") or ""),
                type_mismatch_loser=str(item.get("type_mismatch_loser") or ""),
                followup_edges=list(item.get("followup_edges") or []),
                repair_strategy=str(item.get("repair_strategy") or ""),
                depends_on=list(item.get("depends_on") or []),
                execution_wave=int(item.get("execution_wave") or 0),
                parallel_safe=bool(item.get("parallel_safe")),
                execution_resources=[str(value) for value in item.get("execution_resources") or [] if str(value)],
                execution_serialization_reason=str(item.get("execution_serialization_reason") or ""),
                metadata_tuple_by_host=dict(item.get("metadata_tuple_by_host") or {}),
                metadata_backend_by_host=dict(item.get("metadata_backend_by_host") or {}),
                brick_roles_by_host=dict(item.get("brick_roles_by_host") or {}),
                brick_role_evidence_required=bool(item.get("brick_role_evidence_required")),
                brick_role_evidence_error=str(item.get("brick_role_evidence_error") or ""),
                metadata_majority_hosts=list(item.get("metadata_majority_hosts") or []),
                metadata_mismatch_hosts=list(item.get("metadata_mismatch_hosts") or []),
                metadata_source_host=str(item.get("metadata_source_host") or ""),
                metadata_source_backend=str(item.get("metadata_source_backend") or ""),
                metadata_source_reason=str(item.get("metadata_source_reason") or ""),
                metadata_fields_to_align=list(item.get("metadata_fields_to_align") or []),
                notes=list(item.get("notes") or []),
            )
        )
    return actions
