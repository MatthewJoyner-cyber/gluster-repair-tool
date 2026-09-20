# SPDX-License-Identifier: GPL-2.0-only
"""Guided decision and guarded ready-safe execution for simple repair runs.

The decision layer remains read-only until the operator explicitly authorizes a
ready-safe batch. Execution is delegated to the existing apply-run path;
this module does not maintain a second executor or safety policy.
"""
from __future__ import annotations

import contextlib
import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .apply import (
    build_apply_results,
    load_apply_results,
    load_decisions,
    load_plan,
    write_apply_results,
)
from .apply_planning_utils import (
    _directory_quarantine_target,
    _brick_root_from_backend,
    _file_quarantine_target,
    _gluster_split_brain_preview,
    _step_id,
)
from .apply_reporting import filter_execute_ready_results, is_ready_to_execute, strategy_for_result
from .manifest import load_manifest
from .manager import (
    build_manager_health_report,
    render_manager_preflight_summary,
    resolve_via_backend_path,
    resolve_via_gfid,
    resolve_via_gfid_child,
    resolve_via_index_entry,
    resolve_via_path,
    resolve_via_volume,
    write_manager_preflight_report,
)
from .models import ApplyActionResult, ApplyStep
from .install_paths import DEFAULT_RESOLVER_PATH, DEFAULT_SERVICE_USER, DEFAULT_WORKER_PATH
from .remote_ops import rsync_brick_pull_command
from .role_safety import role_for_host
from .volume import discover_brick_paths
from .planner import build_plan, write_plan
from .shared_io import write_json_shared, write_text_shared
from .simple_assistants import build_simple_assistant_report
from .status import apply_write_history, update_status
from .version import __version__


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_output(stream: TextIO, text: str = "") -> None:
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")
    stream.flush()


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": _now_iso(), **event}, sort_keys=True) + "\n")


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(default)
    return payload if isinstance(payload, dict) else dict(default)


def _recorded_source_brick_resolver_succeeded(report: dict[str, Any], action_id: str) -> bool:
    """Require the exact recorded native resolver to have a conclusive success."""
    for item in report.get("actions") or []:
        if not isinstance(item, dict) or str(item.get("action_id") or "") != action_id:
            continue
        if str(item.get("status") or "") not in {"completed", "completed-with-nonblocking-skips"}:
            return False
        resolver_steps = [
            step
            for step in item.get("steps") or []
            if isinstance(step, dict) and str(step.get("step_type") or "") == "resolve_split_brain_gluster_cli"
        ]
        if len(resolver_steps) != 1:
            return False
        step = resolver_steps[0]
        if str(step.get("status") or "") != "ok":
            return False
        message = str(step.get("message") or "").lower()
        return not any(
            marker in message
            for marker in (
                "no difference in mtime",
                "same mtime",
                "mtime tie",
                "no difference in size",
                "same size",
                "size tie",
            )
        )
    return False


def _recommendation(result: ApplyActionResult) -> tuple[str, str]:
    if result.native_heal_first:
        return (
            "Run the suggested Gluster heal/rescan first, then refresh evidence.",
            result.native_heal_reason
            or "The planner marked this case as native-heal-first; direct repair should wait for fresh evidence.",
        )
    choice = str(result.recommended_choice or "").strip()
    reason = str(result.recommended_reason or "").strip()
    if choice:
        return choice.replace("_", " "), reason or "The planner supplied this preferred choice."
    for note in result.notes:
        for prefix in (
            "matrix suggestion: ",
            "interactive default suggestion: ",
            "preferred choices: ",
        ):
            if note.startswith(prefix):
                return note.removeprefix(prefix), "This guidance came from the existing repair matrix."
    direct_review_edges = {
        "review_entry_split_brain": (
            "Quarantine both conflicting copies",
            "The file or directory winner is not proven; preserve both histories before selecting a GFID, source, or merge path.",
        ),
        "review_directory_gfid_conflict": (
            "Quarantine both directory trees",
            "The directory identity is unresolved; preserve both trees before directory-tie comparison or source selection.",
        ),
        "review_type_mismatch": (
            "Quarantine both file and directory branches",
            "The object type is unresolved; preserve both branches before collecting source-selection evidence.",
        ),
        "review_posix_metadata_no_majority": (
            "Choose an explicit POSIX metadata source",
            "No strict majority exists; select a data-brick source explicitly before aligning metadata.",
        ),
        "review_directory_metadata": (
            "Refresh directory evidence",
            "Refresh child, GFID, mount-access, and metadata evidence before promoting a repair branch.",
        ),
        "review_unclassified_authority": (
            "Refresh and close the authority gap",
            "The planner preserved the candidate but did not classify its write authority, so fresh evidence and review are required before a repair path can exist.",
        ),
        "review_file_metadata": (
            "Enable file-metadata repair policy",
            "Confirm canonical GFID evidence, then rerun with the file-metadata repair policy enabled.",
        ),
        "review_dead_gfid_reference": (
            "Follow the live GFID reference",
            "Resolve the live reference chain before deciding whether residue is removable.",
        ),
        "review_probable_stale_survivor": (
            "Confirm stale-survivor cleanup",
            "Confirm the missing-host and survivor evidence before deleting or salvaging the residue.",
        ),
        "review_probable_orphaned_symlink": (
            "Confirm orphaned-symlink cleanup",
            "Confirm the terminal target and quorum evidence before deleting the symlink residue.",
        ),
    }
    if result.action_type in direct_review_edges:
        return direct_review_edges[result.action_type]
    return "Create a bounded support-case handoff", (
        "This review shape has no local repair proof. Preserve the bounded artifacts and prepare a support handoff "
        "so the case has a concrete continuation rather than ending at review."
    )


def _diagnosis(result: ApplyActionResult) -> str:
    diagnoses = {
        "review_entry_split_brain": "Replica content or entry identity needs an operator decision.",
        "review_type_mismatch": "Replicas disagree about whether the object is a file or directory.",
        "review_posix_metadata_no_majority": "POSIX metadata differs without a strict majority.",
        "review_directory_gfid_conflict": "Directory name/GFID evidence remains ambiguous.",
        "review_directory_children": "Directory child evidence needs follow-up before repair.",
        "review_directory_metadata": "Directory metadata remains unresolved after structural checks.",
        "review_unclassified_authority": "The planner has evidence but no classified authority for a write.",
        "review_file_metadata": "File identity metadata needs policy or operator review.",
        "review_dead_gfid_reference": "A GFID reference still has an unresolved live-reference question.",
        "review_probable_stale_survivor": "A possible stale survivor needs confirmation before cleanup.",
        "review_probable_orphaned_symlink": "A possible orphaned symlink needs confirmation before cleanup.",
    }
    if result.native_heal_first:
        return "Gluster may be able to resolve this case before direct repair is considered."
    return diagnoses.get(
        result.action_type,
        f"The planner produced {result.action_type.replace('_', ' ')} for this object.",
    )


def _confidence(recommendation: str, rationale: str) -> str:
    if not recommendation.strip():
        return "insufficient"
    text = f"{recommendation} {rationale}".lower()
    if "majority" in text:
        return "majority-backed"
    if "proven" in text or "all source" in text:
        return "proven"
    return "heuristic"


def _protection(result: ApplyActionResult) -> str:
    strategy = strategy_for_result(result)
    has_quarantine = any(step.step_type.startswith("quarantine_") for step in result.steps)
    if has_quarantine:
        return (
            "The listed quarantine moves are the reversible preservation for this action; "
            "their recorded revert steps restore the original backend and GFID paths."
        )
    if strategy == "arbiter_backed_data_identity_conflict":
        return (
            "A selected quarantine move will preserve the conflicting data backend and GFID "
            "path as the reversible rollback artifact before the source copy runs."
        )
    if result.backup_mode == "none":
        return "No backup protection is configured for a later execution."
    if result.backup_root:
        return f"Backups are required under {result.backup_root} for a later execution."
    return f"Backup mode is {result.backup_mode}; verify the run's backup gate before execution."


def _risk_class(result: ApplyActionResult) -> str:
    if result.native_heal_first:
        return "chain_follow"
    if result.status == "review":
        return "operator_only"
    if is_ready_to_execute(result):
        return "safe_default"
    return "policy_gated"


@dataclass(frozen=True)
class SimpleDecisionCard:
    decision_id: str
    logical_path: str
    action_type: str
    strategy: str
    safety_class: str
    diagnosis: str
    recommendation: str
    rationale: str
    confidence: str
    impact: str
    protection: str
    notes: tuple[str, ...]
    evidence_fingerprint: str = ""
    assistant: dict[str, Any] = field(default_factory=dict)
    available_choices: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_clear_ready(result: ApplyActionResult) -> bool:
    strategy = strategy_for_result(result)
    return (
        is_ready_to_execute(result)
        and not result.native_heal_first
        and not result.action_type.startswith("review_")
        and "quarantine" not in strategy
    )


def _is_confirmed_operator_action(
    result: ApplyActionResult,
    assistant: dict[str, Any] | None = None,
) -> bool:
    decision = result.decision or {}
    if not (
        bool(decision.get("confirmed"))
        and isinstance(decision.get("decision_payload"), dict)
        and bool(decision.get("decision_payload"))
        and not bool(decision.get("stale"))
    ):
        return False
    saved_fingerprint = str(decision.get("evidence_fingerprint") or "")
    current_fingerprint = str((assistant or {}).get("fingerprint") or "")
    return not (
        saved_fingerprint
        and current_fingerprint
        and saved_fingerprint != current_fingerprint
    )


def _completed_preservation_action_ids(report: dict[str, Any]) -> set[str]:
    completed: set[str] = set()
    for item in report.get("actions") or []:
        if not isinstance(item, dict) or str(item.get("status") or "") not in {
            "completed",
            "completed-with-nonblocking-skips",
        }:
            continue
        decision = item.get("decision") or {}
        decision_payload = (
            decision.get("decision_payload")
            if isinstance(decision, dict)
            else {}
        )
        choices = {
            _normalized_choice(value)
            for value in (decision_payload or {}).values()
        } if isinstance(decision_payload, dict) else set()
        if "quarantine_both" in choices or any(
            str(note).strip().lower() == "quarantine mode: both"
            for note in item.get("notes") or []
        ):
            action_id = str(item.get("action_id") or "")
            if action_id:
                completed.add(action_id)
    return completed


def _is_expected_preservation_terminal(
    result: ApplyActionResult,
    assistant: dict[str, Any],
    completed_preservation_action_ids: set[str],
) -> bool:
    if result.action_id not in completed_preservation_action_ids:
        return False
    action = _choice_action(assistant)
    if str(action.get("repair_strategy") or strategy_for_result(result)) != "review_directory_presence":
        return False
    healthy_hosts = {
        str(host) for host in action.get("healthy_hosts") or [] if str(host)
    }
    missing_hosts = {
        str(host) for host in action.get("missing_hosts") or [] if str(host)
    }
    expected_hosts = {
        str(host)
        for host in (action.get("brick_roles_by_host") or {})
        if str(host)
    }
    return (
        not healthy_hosts
        and bool(missing_hosts)
        and (not expected_hosts or expected_hosts.issubset(missing_hosts))
    )


def _assistant_map(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("action_id") or ""): dict(item)
        for item in (payload or {}).get("items", [])
        if isinstance(item, dict) and str(item.get("action_id") or "")
    }


def _choice_action(assistant: dict[str, Any]) -> dict[str, Any]:
    details = assistant.get("details") or {}
    action = details.get("action") or {}
    return dict(action) if isinstance(action, dict) else {}


def _normalized_choice(value: object) -> str:
    return str(value or "").strip().replace("-", "_").replace(" ", "_").lower()


def _copy_identity(copy: dict[str, Any]) -> str:
    return str(
        copy.get("identity")
        or copy.get("backend_trusted_gfid")
        or copy.get("file_gfid")
        or copy.get("gfid")
        or ""
    ).strip()


def _copy_matches_canonical(
    copy: dict[str, Any],
    *,
    canonical_host: str,
    canonical_backend: str,
    canonical_identity: str,
) -> bool:
    identity = _copy_identity(copy)
    if canonical_identity and identity:
        return identity == canonical_identity
    if canonical_host:
        return str(copy.get("host") or "").strip() == canonical_host
    if canonical_backend:
        return str(copy.get("backend") or "").strip() == canonical_backend
    return False


def _preview_value(value: object) -> str:
    if value in (None, "", {}, []):
        return "not recorded"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _choice_preview(
    action: dict[str, Any],
    *,
    decision: dict[str, Any],
    protection: str,
) -> list[str]:
    """Describe the concrete effect of a supported decision before confirmation."""
    lines: list[str] = []
    action_id = str(action.get("action_id") or "")
    file_copies = [
        copy
        for copy in action.get("file_copies") or []
        if isinstance(copy, dict)
        and str(copy.get("host") or "")
        and str(copy.get("backend") or "")
    ]
    directory_copies = [
        copy
        for copy in action.get("directory_copies") or []
        if isinstance(copy, dict)
        and str(copy.get("host") or "")
        and str(copy.get("backend") or "")
    ]

    source_host = str(
        decision.get("metadata_source_host")
        or decision.get("mdata_source_host")
        or decision.get("source_host")
        or ""
    ).strip()
    if source_host:
        posix_choice = "metadata_source_host" in decision
        backend_key = (
            "metadata_backend_by_host"
            if posix_choice
            else "directory_backend_by_host"
        )
        values_key = (
            "metadata_tuple_by_host"
            if posix_choice
            else "directory_mdata_by_host"
        )
        backends = action.get(backend_key) or {}
        values = action.get(values_key) or {}
        source_backend = str(backends.get(source_host) or "")
        source_value = values.get(source_host)
        scope = (
            "recorded POSIX metadata fields"
            if posix_choice
            else "trusted.glusterfs.mdata"
        )
        lines.append(
            f"effect: use {source_host} as the {scope} source and align only "
            "hosts whose recorded value differs"
        )
        lines.append(
            f"source: {source_host}: {source_backend or 'backend not recorded'}; "
            f"value={_preview_value(source_value)}"
        )
        target_hosts = [
            str(host)
            for host, value in values.items()
            if str(host)
            and str(host) != source_host
            and value not in (None, "", {})
            and value != source_value
        ]
        lines.append("affected targets:")
        for host in sorted(target_hosts):
            role = str((action.get("brick_roles_by_host") or {}).get(host) or "")
            role_note = f"; role={role}" if role else ""
            lines.append(
                f"  - {host}: {backends.get(host) or 'backend not recorded'}; "
                f"current={_preview_value(values.get(host))}; "
                f"proposed={_preview_value(source_value)}{role_note}"
            )
        if not target_hosts:
            lines.append("  - none recorded; planner will keep the case under review")
        lines.append(
            "scope: file content, trusted.gfid identity, and directory children are untouched"
        )
        lines.append(
            "rollback: restore the recorded target metadata from the guarded "
            "batch backup/revert artifacts"
        )

    keep_gfid = str(decision.get("keep_gfid") or "").strip()
    if keep_gfid:
        selected = sorted(
            [
                copy
                for copy in file_copies
                if _copy_identity(copy) == keep_gfid
            ],
            key=lambda copy: (
                int(copy.get("mtime") or -1),
                int(copy.get("size") or -1),
                str(copy.get("host") or ""),
            ),
            reverse=True,
        )
        winner = selected[0] if selected else {}
        lines.append(
            f"effect: select file GFID {keep_gfid} as the identity/content "
            "source for the rebuilt dry-run plan"
        )
        lines.append(
            "selected source: "
            f"{winner.get('host') or 'not recorded'}: "
            f"{winner.get('backend') or 'backend not recorded'}; "
            f"size={winner.get('size', 'not recorded')}; "
            f"mtime={winner.get('mtime', 'not recorded')}"
        )
        lines.append("affected non-selected copies:")
        non_selected = [
            copy for copy in file_copies if _copy_identity(copy) != keep_gfid
        ]
        for copy in non_selected:
            lines.append(
                f"  - {copy.get('host')}: {copy.get('backend')} "
                f"(GFID {_copy_identity(copy) or 'not recorded'})"
            )
        if not non_selected:
            lines.append("  - none recorded")
        lines.append(
            "scope: the planner will choose temp staging, native source-brick, "
            "or loser quarantine from current policy and backup evidence"
        )
        lines.append(
            "rollback: inspect the rebuilt batch's exact backup, quarantine, "
            "and revert steps before authorizing any write"
        )

    if _normalized_choice(decision.get("native_heal_first")) == "refresh":
        lines.append(
            "effect: rerun the original read-only evidence route and give "
            "Gluster native heal/rescan the first opportunity to resolve the item"
        )
        lines.append(
            "affected scope: the current logical path and its recorded heal/GFID "
            "evidence; no source, winner, or quarantine side is selected"
        )
        lines.append(
            "native side effect: a heal crawl or client lookup may cause Gluster "
            "to self-heal even though the repair tool issues no backend write"
        )
        lines.append(
            "rollback: no repair-tool write is made; any native Gluster heal must "
            "be evaluated from the refreshed evidence and Gluster logs"
        )

    file_choice = _normalized_choice(decision.get("file_choice"))
    type_mismatch_choice = _normalized_choice(decision.get("type_mismatch_choice"))
    directory_choice = _normalized_choice(decision.get("directory_choice"))

    quarantine = file_choice or type_mismatch_choice or directory_choice
    if quarantine in {"quarantine_both", "quarantine_loser", "quarantine_side"}:
        if type_mismatch_choice:
            loser_side = str(action.get("type_mismatch_loser") or "").strip().lower()
            if quarantine == "quarantine_both":
                copies = file_copies + directory_copies
            elif loser_side == "file":
                copies = file_copies
            elif loser_side == "directory":
                copies = directory_copies
            else:
                copies = []
            selected = list(copies)
        else:
            copies = file_copies if file_choice else directory_copies
            canonical_host = str(
                action.get("winner_host")
                or action.get("directory_canonical_host")
                or ""
            )
            canonical_backend = str(
                action.get("winner_backend")
                or action.get("directory_canonical_backend")
                or ""
            )
            canonical_identity = str(
                action.get("winner_file_gfid")
                or action.get("directory_canonical_gfid")
                or ""
            )
            selected_identity = str(
                decision.get("directory_quarantine_identity") or ""
            ).strip()
            selected = [
                copy
                for copy in copies
                if quarantine == "quarantine_both"
                or (
                    quarantine == "quarantine_side"
                    and selected_identity
                    and _copy_identity(copy) == selected_identity
                )
                or (
                    quarantine == "quarantine_loser"
                    and not _copy_matches_canonical(
                        copy,
                        canonical_host=canonical_host,
                        canonical_backend=canonical_backend,
                        canonical_identity=canonical_identity,
                    )
                )
            ]
        lines.append(
            f"effect: move {len(selected)} recorded backend object(s) and their "
            f"recorded GFID links aside using {quarantine}"
        )
        lines.append("proposed quarantine targets:")
        for copy in selected:
            host = str(copy.get("host") or "")
            source = str(copy.get("backend") or "")
            identity = _copy_identity(copy)
            is_directory = any(
                copy is candidate for candidate in directory_copies
            )
            target_builder = (
                _directory_quarantine_target
                if is_directory
                else _file_quarantine_target
            )
            target = target_builder(
                source,
                action_id=action_id,
                host=host,
                identity=identity,
                mode=quarantine,
            )
            lines.append(
                f"  - {host}: {source} -> {target} "
                f"({'directory' if is_directory else 'file'}; "
                f"GFID {identity or 'not recorded'})"
            )
            gfid_path = str(
                copy.get("file_gfid_path")
                or copy.get("gfid_path")
                or ""
            ).strip()
            if gfid_path and gfid_path != source:
                gfid_target = target_builder(
                    gfid_path,
                    action_id=action_id,
                    host=host,
                    identity=identity or source,
                    mode=quarantine,
                )
                lines.append(
                    f"    GFID link: {gfid_path} -> {gfid_target}"
                )
        if not selected:
            lines.append("  - none recorded; planner will keep this case under review")
        lines.append(
            "rollback: move every listed quarantine target back to its recorded source path"
        )

    if directory_choice == "merge":
        canonical_gfid = str(action.get("directory_canonical_gfid") or "")
        canonical_host = str(action.get("directory_canonical_host") or "")
        canonical_backend = str(
            action.get("directory_canonical_backend") or ""
        )
        mismatch_copies = [
            copy
            for copy in directory_copies
            if canonical_gfid and _copy_identity(copy) != canonical_gfid
        ]
        lines.append(
            f"effect: attach canonical directory GFID "
            f"{canonical_gfid or 'not recorded'} to the recorded conflicting "
            "directory backends"
        )
        lines.append(
            f"canonical source: {canonical_host or 'not recorded'}: "
            f"{canonical_backend or 'backend not recorded'}"
        )
        lines.append("affected targets:")
        for copy in mismatch_copies:
            lines.append(
                f"  - {copy.get('host')}: {copy.get('backend')} "
                f"(current GFID {_copy_identity(copy) or 'not recorded'}; "
                f"proposed GFID {canonical_gfid or 'not recorded'})"
            )
        if not mismatch_copies:
            lines.append("  - none recorded; merge will remain review-only")
        lines.append(
            "scope: trusted.gfid identity only; the bounded child signatures "
            "already agree and directory children are not rewritten"
        )
        lines.append(
            "rollback: use the guarded batch backup of prior xattrs; inspect the "
            "rebuilt apply preview because merge has no quarantine move-back step"
        )

    if directory_choice == "reconcile_directory_children":
        missing_by_host = action.get("missing_directory_children_by_host") or {}
        dependencies = [
            str(value) for value in action.get("depends_on") or [] if str(value)
        ]
        lines.append(
            "effect: execute only the attached bounded child repairs, then rescan "
            "the parent directory from fresh evidence"
        )
        lines.append(
            "canonical parent: "
            f"{action.get('directory_canonical_host') or 'not recorded'}: "
            f"{action.get('directory_canonical_backend') or 'backend not recorded'}; "
            f"GFID {action.get('directory_canonical_gfid') or 'not recorded'}"
        )
        lines.append("affected child gaps:")
        for host, names in sorted(missing_by_host.items()):
            if not names:
                continue
            lines.append(
                f"  - {host}: {', '.join(str(name) for name in names)}"
            )
        if not any(missing_by_host.values()):
            lines.append("  - none recorded; planner will keep the parent under review")
        lines.append(
            "dependency actions: "
            + (", ".join(dependencies) if dependencies else "none recorded")
        )
        lines.append(
            "scope: the parent directory is not recreated or rewritten by this choice"
        )
        lines.append(
            "rollback: inspect each generated child action's backup/revert steps "
            "before batch authorization"
        )

    if not lines:
        lines.extend(
            [
                "effect: record this evidence-bound choice and rebuild the dry-run plan",
                "affected scope: no concrete target is available until the planner rebuild completes",
                "rollback: no write occurs at this choice stage; inspect the rebuilt batch before authorization",
            ]
        )
    lines.append(f"protection: {protection}")
    return lines


def _operator_choice_specs(
    result: ApplyActionResult,
    assistant: dict[str, Any],
    *,
    protection: str,
) -> tuple[dict[str, Any], ...]:
    """Return only choices that have a corresponding existing planner input."""
    if result.action_type == "review_persistent_split_brain_marker":
        return ()
    action = _choice_action(assistant)
    specs: list[dict[str, Any]] = []
    strategy = str(action.get("repair_strategy") or strategy_for_result(result) or "")
    details = assistant.get("details") or {}

    def add(
        label: str,
        decision: dict[str, Any],
        *,
        risk_class: str,
        why: str,
    ) -> None:
        specs.append(
            {
                "key": str(len(specs) + 1),
                "label": label,
                "decision": decision,
                "risk_class": risk_class,
                "preserve_first": True,
                "requires_confirmation": True,
                "why": why,
                "preview": _choice_preview(action, decision=decision, protection=protection),
            }
        )

    if result.native_heal_first:
        add(
            "Run the native Gluster heal/rescan first",
            {"native_heal_first": "refresh"},
            risk_class="chain_follow",
            why=(
                result.native_heal_reason
                or "The planner says Gluster should be given the first opportunity to resolve this case."
            ),
        )
    if not action:
        return tuple(specs)

    if result.action_type == "review_posix_metadata_no_majority":
        posix = details.get("posix") or {}
        for row in posix.get("rows") or []:
            if not isinstance(row, dict):
                continue
            host = str(row.get("host") or "")
            role = str(row.get("role") or "").lower()
            backend = str(row.get("backend") or "")
            if not host or role == "arbiter" or not backend or not row.get("tuple"):
                continue
            add(
                f"Use POSIX metadata from {host}",
                {"metadata_source_host": host},
                risk_class="source_selection",
                why="The planner will align only the recorded POSIX metadata fields from this source host.",
            )


    if strategy == "review_directory_mdata_state":
        roles = action.get("brick_roles_by_host") or {}
        aliases = action.get("brick_host_aliases") or {}
        mdata = action.get("directory_mdata_by_host") or {}
        backends = action.get("directory_backend_by_host") or {}
        for host in sorted(mdata):
            host_text = str(host)
            if not host_text or not mdata.get(host) or role_for_host(roles, host_text, aliases) == "arbiter":
                continue
            if not backends.get(host):
                continue
            add(
                f"Use directory metadata from {host}",
                {"directory_choice": "repair_directory_mdata", "mdata_source_host": host_text},
                risk_class="source_selection",
                why="The planner will align trusted.glusterfs.mdata only; it will not change trusted.gfid or children.",
            )

    if result.action_type == "review_entry_split_brain":
        checksum = details.get("checksum") or {}
        copies = action.get("file_copies") or []
        identities: list[str] = []
        for copy in copies:
            if not isinstance(copy, dict):
                continue
            identity = _copy_identity(copy)
            if identity and identity not in identities:
                identities.append(identity)
        if str(checksum.get("outcome") or "") == "content-equal":
            for identity in identities:
                hosts = sorted(
                    str(copy.get("host") or "")
                    for copy in copies
                    if isinstance(copy, dict) and _copy_identity(copy) == identity
                )
                add(
                    f"Preserve file identity {identity} ({', '.join(hosts) or 'host not recorded'})",
                    {"keep_gfid": identity},
                    risk_class="source_selection",
                    why="Checksums match, so this preserves one existing GFID identity without choosing different content.",
                )
        elif str(checksum.get("outcome") or "") == "content-different" and copies:
            add(
                "Quarantine both file copies for review",
                {"file_choice": "quarantine_both"},
                risk_class="quarantine",
                why="Checksums differ; preserving both histories is safer than silently selecting content.",
            )
        if (
            copies
            and _normalized_choice(action.get("recommended_choice")) == "quarantine_loser"
            and not any(
                _normalized_choice(spec.get("decision", {}).get("file_choice")) == "quarantine_loser"
                for spec in specs
            )
        ):
            conflicts = ", ".join(str(host) for host in action.get("conflict_hosts") or [] if str(host))
            add(
                f"Quarantine the conflicting data copy{f' ({conflicts})' if conflicts else ''}",
                {"file_choice": "quarantine_loser"},
                risk_class="quarantine",
                why="The matching data brick plus arbiter identity is the tie-breaker; quarantine preserves the divergent copy before the explicit source copy runs.",
            )
        if copies and not any(
            _normalized_choice(spec.get("decision", {}).get("file_choice"))
            in {"quarantine_both", "quarantine_loser"}
            for spec in specs
        ):
            add(
                "Quarantine both file copies for review",
                {"file_choice": "quarantine_both"},
                risk_class="quarantine",
                why="The file winner is not proven; preserve both histories before selecting a GFID or source.",
            )

    if result.action_type == "review_type_mismatch" and action.get("file_copies") and action.get("directory_copies"):
        loser_side = str(action.get("type_mismatch_loser") or "").strip().lower()
        if (
            _normalized_choice(action.get("recommended_choice")) == "quarantine_loser"
            and loser_side in {"file", "directory"}
        ):
            add(
                f"Quarantine the older {loser_side} branch",
                {"type_mismatch_choice": "quarantine_loser"},
                risk_class="quarantine",
                why=(
                    f"Complete non-overlapping backend mtime ranges identify the {loser_side} branch as older; "
                    "the newer branch remains in place without being declared authoritative."
                ),
            )
        add(
            "Quarantine both file and directory branches",
            {"type_mismatch_choice": "quarantine_both"},
            risk_class="quarantine",
            why="The object type is unresolved; the supported reversible choice preserves both branches for review.",
        )

    if result.action_type == "review_directory_gfid_conflict":
        copies = action.get("directory_copies") or []
        if copies:
            cohorts: dict[str, list[dict[str, Any]]] = {}
            for copy in copies:
                if not isinstance(copy, dict):
                    continue
                identity = _copy_identity(copy)
                if identity:
                    cohorts.setdefault(identity, []).append(copy)
            if len(cohorts) > 1:
                for index, (identity, cohort) in enumerate(
                    sorted(cohorts.items()),
                    start=1,
                ):
                    hosts = sorted(
                        str(copy.get("host") or "")
                        for copy in cohort
                        if str(copy.get("host") or "")
                    )
                    side = chr(ord("A") + index - 1) if index <= 26 else str(index)
                    add(
                        f"Quarantine side {side}: GFID {identity} "
                        f"({', '.join(hosts)})",
                        {
                            "directory_choice": "quarantine_side",
                            "directory_quarantine_identity": identity,
                        },
                        risk_class="quarantine",
                        why=(
                            "Moves only this explicitly identified directory cohort aside. "
                            "No authority is inferred for either side."
                        ),
                    )
            add(
                "Quarantine both directory sides",
                {"directory_choice": "quarantine_both"},
                risk_class="quarantine",
                why="Preserves both divergent directory histories without selecting a winner.",
            )
            signatures = {
                tuple(sorted(str(name) for name in names))
                for names in (action.get("directory_child_names_by_host") or {}).values()
                if isinstance(names, list) and names
            }
            if (
                action.get("directory_canonical_gfid")
                and action.get("directory_canonical_backend")
                and not action.get("missing_hosts")
                and len(signatures) == 1
            ):
                add(
                    "Merge the shared directory identity",
                    {"directory_choice": "merge"},
                    risk_class="risky_default",
                    why="The bounded child signatures agree and the planner can use its existing canonical GFID attach path.",
                )

    if result.action_type == "review_directory_children":
        if (
            action.get("depends_on")
            and "directory_child_gap:executable" in (action.get("graph_markers") or [])
        ):
            add(
                "Apply the bounded child-gap suggestion",
                {"directory_choice": "reconcile_directory_children"},
                risk_class="risky_default",
                why="The planner has attached a complete set of executable child repairs; the parent only waits for those repairs and a fresh rescan.",
            )

    return tuple(specs)


def _assisted_recommendation(
    result: ApplyActionResult,
    assistant: dict[str, Any],
) -> tuple[str, str]:
    if result.action_type == "review_persistent_split_brain_marker":
        return (
            "Create a bounded support-case handoff",
            "Two checksum-equal data-brick source resolvers completed, but Gluster still reports the canonical GFID. Preserve the evidence and stop rather than retrying another source or quarantining matching copies.",
        )
    recommendation, rationale = _recommendation(result)
    if (
        strategy_for_result(result) == "arbiter_backed_data_identity_conflict"
        and result.recommended_choice
    ):
        return recommendation, rationale
    details = assistant.get("details") or {}
    checksum = details.get("checksum") or {}
    outcome = str(checksum.get("outcome") or "")
    if outcome == "content-equal":
        return (
            "Quarantine both file copies",
            "The checksum assistant found matching content, but equal content does not by itself choose a GFID or source brick; preserve both identities before selection.",
        )
    if outcome == "content-different":
        return (
            "Quarantine both file copies",
            "The checksum assistant confirmed different content; preserve both histories through quarantine_both before choosing a source.",
        )
    directory = details.get("directory_tie") or {}
    classification = directory.get("classification") or {}
    branch_type = str(
        directory.get("branch_type")
        or classification.get("branch_type")
        or ""
    )
    missing_hosts = [
        str(host)
        for host in classification.get("missing_hosts") or []
        if str(host)
    ]
    if branch_type == "missing" and missing_hosts:
        return (
            "Recover from an authoritative backup, or skip to accept the intentional absence.",
            (
                "Fresh evidence found no directory copy on any recorded brick, "
                "so there is nothing left to quarantine or select as a source."
            ),
        )
    directory_choice = str(directory.get("recommended_choice") or "")
    if directory_choice and directory_choice not in {"defer", "keep_review"}:
        return (
            f"Directory tie report recommends {directory_choice.replace('_', ' ')}; explicit review is still required.",
            str(directory.get("classification", {}).get("reason") or "The bounded directory-tie report supplied this recommendation."),
        )
    live = details.get("live_reference") or {}
    if live.get("live_references"):
        return (
            "Follow the live GFID reference",
            "A live-reference target remains in the evidence; resolve that path before deciding whether GFID or index residue is removable.",
        )
    return recommendation, rationale


def build_simple_decision_cards(
    results: list[ApplyActionResult],
    assistants: dict[str, Any] | None = None,
    executed_action_ids: set[str] | None = None,
    completed_preservation_action_ids: set[str] | None = None,
) -> tuple[list[SimpleDecisionCard], list[ApplyActionResult]]:
    cards: list[SimpleDecisionCard] = []
    ready_batch: list[ApplyActionResult] = []
    assistant_by_id = _assistant_map(assistants)
    executable_results, _skipped_results = filter_execute_ready_results(results)
    executable_ids = {result.action_id for result in executable_results}
    executed_action_ids = executed_action_ids or set()
    completed_preservation_action_ids = completed_preservation_action_ids or set()
    for result in results:
        assistant = assistant_by_id.get(result.action_id, {})
        if _is_expected_preservation_terminal(
            result,
            assistant,
            completed_preservation_action_ids,
        ):
            continue
        if (
            result.action_id in executable_ids
            and result.action_id not in executed_action_ids
            and (
                _is_clear_ready(result)
                or _is_confirmed_operator_action(result, assistant)
            )
        ):
            ready_batch.append(result)
            continue
        recommendation, rationale = _assisted_recommendation(result, assistant)
        if result.action_id in executed_action_ids:
            recommendation = "Review refreshed evidence before any retry"
            rationale = "This action was already executed in the current run; the refreshed evidence must be reviewed before it can be considered again."
        assistant_notes = [
            str(note)
            for note in assistant.get("evidence", [])
            if str(note)
        ]
        protection = _protection(result)
        saved_decision_active = _is_confirmed_operator_action(result, assistant)
        cards.append(
            SimpleDecisionCard(
                decision_id=result.action_id,
                logical_path=result.logical_path,
                action_type=result.action_type,
                strategy=strategy_for_result(result),
                safety_class=_risk_class(result),
                diagnosis=_diagnosis(result),
                recommendation=recommendation,
                rationale=rationale,
                confidence=_confidence(recommendation, rationale),
                impact=(
                    "Stage 1 makes no Gluster or filesystem changes. "
                    "A later decision may affect this object and its listed brick copies."
                ),
                protection=protection,
                notes=tuple(
                    [str(note) for note in result.notes if str(note)]
                    + assistant_notes
                ),
                evidence_fingerprint=str(assistant.get("fingerprint") or ""),
                assistant=assistant,
                available_choices=(
                    ()
                    if saved_decision_active
                    else _operator_choice_specs(
                        result,
                        assistant,
                        protection=protection,
                    )
                ),
            )
        )
    return cards, ready_batch


def _card_payload(
    cards: list[SimpleDecisionCard],
    ready_batch: list[ApplyActionResult],
    *,
    previous: dict[str, Any] | None = None,
    write_occurred: bool = False,
) -> dict[str, Any]:
    previous = previous or {}
    position = int(previous.get("position") or 0)
    position = max(0, min(position, len(cards)))
    write_outcome_unknown = bool(previous.get("write_outcome_unknown"))
    return {
        "schema_version": 1,
        "mode": "write-unknown" if write_outcome_unknown else ("executed" if write_occurred else "read-only"),
        "state": "pending" if cards else "clear",
        "position": position,
        "decision_count": len(cards),
        "decision_queue": [card.to_dict() for card in cards],
        "ready_batch": {
            "count": len(ready_batch),
            "action_ids": [item.action_id for item in ready_batch],
            "logical_paths": [item.logical_path for item in ready_batch],
            "write_confirmation_required_later": bool(ready_batch),
        },
        "write_occurred": bool(write_occurred),
        "write_outcome_unknown": write_outcome_unknown,
        "updated_at": _now_iso(),
    }


def _apply_interaction_write_history(
    interaction: dict[str, Any],
    *records: object,
) -> dict[str, Any]:
    """Keep the interaction's rendered mode aligned with immutable write facts."""
    history = apply_write_history(interaction, *records)
    if history["write_outcome_unknown"]:
        interaction["mode"] = "write-unknown"
    elif history["write_occurred"]:
        interaction["mode"] = "executed"
    else:
        interaction["mode"] = "read-only"
    return history


def _save_decision(
    paths: Any,
    card: SimpleDecisionCard,
    choice: str,
    *,
    position: int,
    decision: dict[str, Any] | None = None,
    choice_spec: dict[str, Any] | None = None,
    confirmed: bool = False,
) -> None:
    payload = _load_json(Path(paths.decisions), {"schema_version": 1, "decisions": {}})
    decisions = payload.setdefault("decisions", {})
    if not isinstance(decisions, dict):
        decisions = {}
        payload["decisions"] = decisions
    entry: dict[str, Any] = {
        "logical_path": card.logical_path,
        "operator_choice": choice,
        "recommendation": card.recommendation,
        "rationale": card.rationale,
        "confidence": card.confidence,
        "safety_class": card.safety_class,
        "recorded_at": _now_iso(),
        "read_only": True,
        "position_after": position,
        "evidence_fingerprint": card.evidence_fingerprint,
        "topology_fingerprint": str(card.assistant.get("topology_fingerprint") or ""),
    }
    if decision:
        entry.update(dict(decision))
        entry["decision_payload"] = dict(decision)
        entry["confirmed"] = bool(confirmed)
        entry["preserve_first"] = bool((choice_spec or {}).get("preserve_first", True))
        entry["risk_class"] = str((choice_spec or {}).get("risk_class") or card.safety_class)
        entry["choice_spec"] = dict(choice_spec or {})
    else:
        entry["choice"] = choice
    decision_key = card.logical_path if decision else card.decision_id
    decisions[decision_key] = entry
    history = payload.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        payload["history"] = history
    history.append(
        {
            "decision_id": card.decision_id,
            "choice": choice,
            "decision": dict(decision or {}),
            "confirmed": bool(confirmed),
            "at": _now_iso(),
        }
    )
    write_json_shared(paths.decisions, payload)


def _invalidate_stale_choices(
    paths: Any,
    cards: list[SimpleDecisionCard],
) -> list[str]:
    payload = _load_json(Path(paths.decisions), {"schema_version": 1, "decisions": {}})
    decisions = payload.get("decisions")
    if not isinstance(decisions, dict):
        return []
    stale: list[str] = []
    changed = False
    for card in cards:
        saved = decisions.get(card.logical_path) or decisions.get(card.decision_id)
        if not isinstance(saved, dict):
            continue
        previous_fingerprint = str(saved.get("evidence_fingerprint") or "")
        current_fingerprint = str(card.evidence_fingerprint or "")
        if not previous_fingerprint or not current_fingerprint or previous_fingerprint == current_fingerprint:
            continue
        if bool(saved.get("stale")):
            continue
        saved["stale"] = True
        saved["invalidation_reason"] = (
            "read-only evidence fingerprint changed; the prior choice must be reviewed again"
        )
        saved["invalidated_at"] = _now_iso()
        stale.append(card.decision_id)
        changed = True
    if changed:
        write_json_shared(paths.decisions, payload)
    return stale


def _invalidate_and_report_stale_choices(
    paths: Any,
    cards: list[SimpleDecisionCard],
    output_stream: TextIO,
) -> list[str]:
    stale_choices = _invalidate_stale_choices(paths, cards)
    if not stale_choices:
        return []
    _append_event(
        Path(paths.events),
        {
            "event": "choices-invalidated",
            "decision_ids": stale_choices,
            "reason": "evidence fingerprint changed",
        },
    )
    _write_output(
        output_stream,
        f"Invalidated {len(stale_choices)} saved choice(s) because read-only evidence changed.",
    )
    return stale_choices


def _first_stale_position(
    cards: list[SimpleDecisionCard],
    stale_choices: list[str],
) -> int:
    stale_ids = set(stale_choices)
    return next(
        (
            index
            for index, card in enumerate(cards)
            if card.decision_id in stale_ids
        ),
        0,
    )


def _support_summary(paths: Any, summary: dict[str, Any], card: SimpleDecisionCard) -> str:
    status = _load_json(Path(paths.status), {})
    artifacts = dict(summary.get("artifacts") or {})
    for name in (
        "status",
        "health",
        "manifest",
        "observations",
        "plan",
        "apply",
        "execute_results",
        "decisions",
        "summary",
        "assistants",
    ):
        if not artifacts.get(name) and getattr(paths, name, None):
            artifacts[name] = str(getattr(paths, name))
    artifacts.setdefault("execute_results", str(Path(paths.root) / "execute-results.json"))
    lines = [
        "Gluster repair support-case handoff",
        "===================================",
        f"run directory: {summary.get('run_dir', paths.root)}",
        f"volume: {summary.get('volume', 'unknown')}",
        f"logical path: {card.logical_path}",
        f"action type: {card.action_type}",
        f"strategy: {card.strategy or 'not provided'}",
        f"tool version: {__version__}",
        f"safety class: {card.safety_class}",
        f"diagnosis: {card.diagnosis}",
        f"recommendation: {card.recommendation}",
        f"confidence: {card.confidence}",
        f"rationale: {card.rationale}",
        f"protection: {card.protection}",
        f"health: {summary.get('health_summary') or status.get('health_summary') or 'see health artifact'}",
        f"evidence: {summary.get('evidence_summary') or status.get('manifest_summary') or 'see manifest and observations artifacts'}",
        f"plan: {summary.get('plan_summary') or status.get('plan_summary') or 'see plan artifact'}",
        f"apply preview: {summary.get('apply_summary') or status.get('apply_summary') or 'see apply artifact'}",
        f"evidence fingerprint: {card.evidence_fingerprint or 'not recorded'}",
        f"write occurred: {'yes' if summary.get('write_occurred') or status.get('write_occurred') else 'no'}",
        f"support summary: {paths.support_summary}",
        "",
        "Relevant artifacts:",
    ]
    for name in (
        "status",
        "health",
        "manifest",
        "observations",
        "plan",
        "apply",
        "execute_results",
        "decisions",
        "summary",
        "assistants",
    ):
        if artifacts.get(name):
            artifact_path = Path(artifacts[name])
            state = "present" if artifact_path.is_file() else "missing"
            lines.append(f"  {name}: {artifact_path} ({state}; not yet redacted)")
    if card.action_type == "review_persistent_split_brain_marker":
        lines.extend(
            [
                "Terminal condition: both checksum-equal data-brick source resolvers completed, but Gluster retained the canonical GFID marker.",
                "Do not retry source selection or quarantine matching data copies.",
                "Include the resolver execution record and role evidence with the bounded artifacts.",
            ]
        )
    lines.extend(
        [
            "",
            "The run reached an operator decision; this is a local draft, not a submitted support case.",
            "Open a Gluster/vendor support case only after copying, redacting and reviewing present bounded evidence.",
            "Do not attach file payloads, credentials, or unrelated paths without review.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_card(card: SimpleDecisionCard, index: int, total: int, stream: TextIO) -> None:
    choice_lines = [
        "",
        "Supported quarantine/source choices (each requires typing CONFIRM):",
    ]
    if card.available_choices:
        for spec in card.available_choices:
            choice_lines.append(f"  [{spec['key']}] {spec['label']}")
    else:
        choice_lines.append(
            "  (none; use skip, defer, or support handoff for this sticking point)"
        )
    _write_output(
        stream,
        "\n".join(
            [
                f"Decision {index} of {total}: {card.logical_path}",
                f"Diagnosis: {card.diagnosis}",
                f"Safety: {card.safety_class}",
                f"Evidence route: {card.action_type.replace('_', ' ')}"
                + (f" ({card.strategy})" if card.strategy else ""),
                f"Recommendation: {card.recommendation}",
                f"Confidence: {card.confidence}",
                f"Why: {card.rationale}",
                f"Impact: {card.impact}",
                f"Protection: {card.protection}",
                f"Evidence fingerprint: {card.evidence_fingerprint or 'not recorded'}",
                *choice_lines,
                "",
                "  [?] explain the diagnosis and recommendation",
                "  [e] inspect bounded evidence",
                "  [p] inspect proposed planner/apply steps",
                "  [b] inspect backup and rollback coverage",
                "  [t] retry health checks",
                "  [x] refresh read-only evidence",
                "  [n] replan from the same evidence",
                "  [r] record the recommendation for later review",
                "  [s] skip this item (no changes)",
                "  [d] defer this sticking point",
                "  [h] create a support-case handoff",
                "  [q] save and quit",
            ]
        ),
    )


def _render_detail(card: SimpleDecisionCard, kind: str, stream: TextIO) -> None:
    if kind == "e":
        _write_output(stream, "Evidence notes:")
        for note in card.notes or ("No additional bounded evidence notes were recorded.",):
            _write_output(stream, f"  - {note}")
    elif kind == "p":
        _write_output(
            stream,
            f"Planner/apply action: {card.action_type}; strategy: {card.strategy or 'not provided'}",
        )
        _write_output(stream, "This card does not invoke execution directly; batch authorization is handled only at the batch gate.")
    elif kind == "b":
        _write_output(stream, f"Protection: {card.protection}")
        if card.strategy == "arbiter_backed_data_identity_conflict":
            _write_output(stream, "The selected quarantine path becomes the named rollback artifact during the later guarded batch.")
        else:
            _write_output(stream, "This card does not create a new rollback artifact; batch-level protection is handled by the canonical apply-run gate.")
    elif kind == "t":
        _write_output(stream, "Health retry is read-only and checks current host/volume readiness again.")
        _write_output(stream, "If warnings remain, simple mode will stop before any execution stage.")
    elif kind == "x":
        _write_output(stream, "Evidence refresh reruns the original heal/path/GFID/index discovery route, then rebuilds the plan and assistants.")
    elif kind == "n":
        _write_output(stream, "Same-source replan rebuilds plan and dry-run apply artifacts from the current manifest; it does not touch Gluster data.")
    else:
        _write_output(stream, f"Why this is a sticking point: {card.rationale}")


def _confirm_operator_choice(
    card: SimpleDecisionCard,
    spec: dict[str, Any],
    *,
    input_stream: TextIO,
    output_stream: TextIO,
) -> bool:
    _write_output(output_stream, f"Selected: {spec['label']}")
    _write_output(output_stream, f"Why: {spec['why']}")
    _write_output(output_stream, "Proposed effect:")
    for line in spec.get("preview") or []:
        _write_output(output_stream, f"  {line}")
    _write_output(
        output_stream,
        "This is preserve-first and remains a dry-run decision until the later guarded execution gate.",
    )
    _write_output(output_stream, "Type CONFIRM exactly to record this decision, or anything else to cancel.")
    try:
        answer = input_stream.readline()
    except KeyboardInterrupt:
        answer = ""
    if answer.strip() != "CONFIRM":
        _write_output(output_stream, "Choice cancelled; no decision was recorded.")
        return False
    return True


def _path(paths: Any, name: str, suffix: str) -> Path:
    value = getattr(paths, name, None)
    return Path(value) if value else Path(paths.root) / suffix


def _assistant_report(paths: Any, summary: dict[str, Any]) -> dict[str, Any]:
    plan = load_plan(paths.plan)
    report = build_simple_assistant_report(
        plan,
        volume=str(summary.get("volume") or ""),
        worker_path=str(summary.get("worker_path") or DEFAULT_WORKER_PATH),
        ssh_user=str(summary.get("ssh_user") or DEFAULT_SERVICE_USER),
        connect_timeout=float(summary.get("connect_timeout") or 10.0),
        status=_load_json(Path(paths.status), {}),
    )
    write_json_shared(_path(paths, "assistants", "assistants.json"), report)
    update_status(
        paths.status,
        phase="simple-interactive-assistants-refreshed",
        assistants_out=str(_path(paths, "assistants", "assistants.json")),
        assistant_summary=report.get("summary", {}),
        assistant_plan_fingerprint=report.get("plan_fingerprint", ""),
        assistant_topology_fingerprint=report.get("topology_fingerprint", ""),
    )
    return report


def _same_source_replan(
    paths: Any,
    summary: dict[str, Any],
) -> tuple[list[ApplyActionResult], dict[str, Any]]:
    manifest_payload = load_plan(paths.manifest)
    actions = build_plan(
        load_manifest(paths.manifest, payload=manifest_payload),
        mountpoint=str(summary.get("mountpoint") or "/volume"),
        split_brain_policy=str(summary.get("split_brain_policy") or "auto"),
    )
    write_plan(paths.plan, actions, manifest_in=paths.manifest, manifest_payload=manifest_payload)
    try:
        decisions = load_decisions(paths.decisions)
    except (OSError, ValueError):
        decisions = {}
    plan_payload = load_plan(paths.plan)
    results = build_apply_results(
        plan_payload,
        execution_mode="dry-run",
        backup_root=str(paths.backup),
        backup_mode=str(summary.get("backup_mode") or "required"),
        batch=False,
        volume=str(summary.get("volume") or ""),
        brick_path=str(summary.get("brick_path") or ""),
        worker_path=str(summary.get("worker_path") or DEFAULT_WORKER_PATH),
        ssh_user=str(summary.get("ssh_user") or DEFAULT_SERVICE_USER),
        decisions=decisions,
    )
    write_apply_results(
        paths.apply,
        results,
        decision_file=str(paths.decisions),
        controller_cycle={},
        plan_in=paths.plan,
        plan_payload=plan_payload,
    )
    assistant_report = _assistant_report(paths, summary)
    update_status(
        paths.status,
        phase="simple-interactive-replanned",
        write_occurred=bool(summary.get("write_occurred")),
        plan_out=str(paths.plan),
        apply_out=str(paths.apply),
    )
    return results, assistant_report


def _retry_health(paths: Any, summary: dict[str, Any], output_stream: TextIO) -> None:
    health = build_manager_health_report(
        str(summary.get("volume") or ""),
        ssh_user=str(summary.get("ssh_user") or DEFAULT_SERVICE_USER),
        connect_timeout=float(summary.get("connect_timeout") or 10.0),
    )
    write_manager_preflight_report(paths.health, health)
    update_status(
        paths.status,
        phase="simple-interactive-health-retry",
        health_out=str(paths.health),
        health_check=health,
        health_check_checked_at=health.get("checked_at", ""),
        write_occurred=bool(summary.get("write_occurred")),
    )
    _write_output(output_stream, "Health retry complete:")
    _write_output(output_stream, render_manager_preflight_summary(health))


def _refresh_evidence(paths: Any, summary: dict[str, Any]) -> tuple[list[ApplyActionResult], dict[str, Any]]:
    inputs = summary.get("evidence_inputs") or {}
    common = {
        "resolver_path": str(summary.get("resolver_path") or DEFAULT_RESOLVER_PATH),
        "worker_path": str(summary.get("worker_path") or DEFAULT_WORKER_PATH),
        "manifest_out": str(paths.manifest),
        "observations_out": str(paths.observations),
        "ssh_user": str(summary.get("ssh_user") or DEFAULT_SERVICE_USER),
        "log_path": str(paths.root / "evidence.log"),
        "verbose": bool(summary.get("verbose")),
    }
    volume = str(summary.get("volume") or "")
    mountpoint = str(summary.get("mountpoint") or f"/{volume}")
    if inputs.get("path"):
        resolve_via_path(path=str(inputs["path"]), probe_mount=True, **common)
    elif inputs.get("backend_path"):
        resolve_via_backend_path(
            volume=volume,
            backend_path=str(inputs["backend_path"]),
            mountpoint=mountpoint,
            **common,
        )
    elif inputs.get("gfid"):
        resolve_via_gfid(volume=volume, gfid=str(inputs["gfid"]), mountpoint=mountpoint, **common)
    elif inputs.get("gfid_child"):
        resolve_via_gfid_child(
            volume=volume,
            gfid_child=str(inputs["gfid_child"]),
            mountpoint=mountpoint,
            **common,
        )
    elif inputs.get("index_entry"):
        resolve_via_index_entry(
            volume=volume,
            index_entry=str(inputs["index_entry"]),
            mountpoint=mountpoint,
            **common,
        )
    else:
        brick_paths = discover_brick_paths(volume)
        brick_path = next(iter(dict.fromkeys(brick_paths.values())), "")
        resolve_via_volume(
            heal_file=inputs.get("heal_file"),
            volume=volume,
            brick_path=brick_path,
            brick_paths=brick_paths,
            mountpoint=mountpoint,
            heal_out=str(paths.heal_before),
            heal_root=str(paths.root / "heal-info"),
            heal_latest=bool(inputs.get("heal_latest")),
            heal_fresh=True,
            **common,
        )
    update_status(
        paths.status,
        phase="simple-interactive-evidence-refreshed",
        manifest_out=str(paths.manifest),
        observations_out=str(paths.observations),
        write_occurred=bool(summary.get("write_occurred")),
    )
    return _same_source_replan(paths, summary)


def _granular_handoff(paths: Any) -> str:
    return (
        "Granular handoff:\n"
        f"  plan: gluster-manager plan-build --manifest-in {paths.manifest} "
        f"--plan-out {paths.plan} --status-file {paths.status}\n"
        f"  apply: gluster-manager apply-build --plan-in {paths.plan} "
        f"--apply-out {paths.apply} --backup-root {paths.backup} --status-file {paths.status}\n"
    )


def _step_scope(step: Any) -> str:
    parts = []
    if str(step.host or ""):
        parts.append(f"host={step.host}")
    if str(step.source_host or ""):
        parts.append(f"source_host={step.source_host}")
    if str(step.source_path or ""):
        parts.append(f"source={step.source_path}")
    if str(step.target_path or ""):
        parts.append(f"target={step.target_path}")
    if bool(step.via_mount):
        parts.append("via_mount=yes")
    if bool(step.tolerate_missing):
        parts.append("missing=tolerated")
    return "; ".join(parts) or "no path scope recorded"


def _render_ready_batch(results: list[ApplyActionResult], stream: TextIO) -> None:
    stage_bytes = sum(int(result.estimated_stage_bytes or 0) for result in results)
    backup_bytes = sum(int(result.estimated_backup_bytes or 0) for result in results)
    unknown_backups = sum(int(result.estimated_unknown_backup_items or 0) for result in results)
    waves = sorted({int(result.execution_wave) for result in results})
    operator_approved = any(_is_confirmed_operator_action(result) for result in results)
    heading = "Operator-approved batch preview" if operator_approved else "Ready-safe batch preview"
    _write_output(stream, heading)
    _write_output(stream, "=" * len(heading))
    _write_output(stream, f"Actions: {len(results)}; execution waves: {len(waves)}")
    _write_output(stream, f"Estimated staging bytes: {stage_bytes}")
    _write_output(stream, f"Estimated backup bytes: {backup_bytes}")
    _write_output(stream, f"Backup items with unknown size: {unknown_backups}")
    _write_output(stream, "The existing apply-run gates will recheck health, dependencies, controller-cycle state, backups, and heal handling before any write.")
    for number, result in enumerate(results, 1):
        _write_output(
            stream,
            f"{number}. {result.logical_path} [{result.action_type}] "
            f"strategy={strategy_for_result(result) or 'not recorded'} "
            f"wave={result.execution_wave}",
        )
        _write_output(stream, "   apply steps:")
        for step in result.steps:
            command = (
                " ".join(step.command_preview)
                if step.command_preview
                else "(no shell command preview)"
            )
            _write_output(
                stream,
                f"   - {step.step_type}: {_step_scope(step)}; command={command}",
            )
        if not result.steps:
            _write_output(stream, "   - none; this action cannot execute")
        _write_output(stream, "   rollback/revert steps:")
        for step in result.revert_steps:
            command = (
                " ".join(step.command_preview)
                if step.command_preview
                else "(no shell command preview)"
            )
            _write_output(
                stream,
                f"   - {step.step_type}: {_step_scope(step)}; command={command}",
            )
        if not result.revert_steps:
            _write_output(
                stream,
                "   - no standalone revert step; use the listed backup artifacts and verification gate",
            )
        _write_output(stream, "   backup artifacts:")
        for artifact in result.backup_artifacts:
            estimated = (
                str(artifact.estimated_bytes)
                if artifact.estimated_bytes is not None
                else "unknown"
            )
            _write_output(
                stream,
                f"   - {artifact.host}: {artifact.source_path} -> "
                f"{artifact.backup_path}; kind={artifact.kind}; "
                f"bytes={estimated}; required_for_revert="
                f"{'yes' if artifact.required_for_revert else 'no'}",
            )
        if not result.backup_artifacts:
            has_quarantine = any(step.step_type.startswith("quarantine_") for step in result.steps)
            if has_quarantine:
                _write_output(
                    stream,
                    "   - no separate backup copy is planned: the named quarantine targets above retain the "
                    "conflicting backend and GFID link for rollback",
                )
            elif any(
                marker in str(note)
                for note in result.notes
                for marker in ("post-copy marker continuation", "alternate-source marker continuation")
            ):
                _write_output(
                    stream,
                    "   - no new backup is needed: the earlier named quarantine targets remain the rollback protection "
                    "for the replaced conflicting copy",
                )
            else:
                _write_output(
                    stream,
                    f"   - none preplanned; backup mode={result.backup_mode}, "
                    f"backup root={result.backup_root or 'not recorded'}",
                )
    _write_output(
        stream,
        "Verification: execution must pass the canonical post-write checks and fresh-evidence replan before another batch.",
    )
    if any(
        any(step.step_type.startswith("quarantine_") for step in result.steps)
        or any(
            marker in str(note)
            for note in result.notes
            for marker in ("post-copy marker continuation", "alternate-source marker continuation")
        )
        for result in results
    ):
        _write_output(stream, "Protection: named quarantine targets and recorded revert steps preserve the original copies; execution gates remain in force.")
    else:
        _write_output(stream, "Protection: backups and the existing reversible execution gates remain in force.")


def _execution_action_ids(report: dict[str, Any]) -> set[str]:
    return {
        str(item.get("action_id") or "")
        for item in report.get("actions", [])
        if isinstance(item, dict) and str(item.get("action_id") or "")
        and str(item.get("status") or "") in {"completed", "completed-with-nonblocking-skips", "failed"}
    }


def _post_preservation_file_continuation(
    paths: Any,
    results: list[ApplyActionResult],
) -> tuple[list[ApplyActionResult], dict[str, Any]] | None:
    """Return the tightly bounded retry allowed after a preserve-first copy failure."""
    report = _load_json(Path(paths.root) / "execute-results.json", {})
    failed = [
        item
        for item in report.get("actions") or []
        if isinstance(item, dict) and str(item.get("status") or "") == "failed"
    ]
    if len(failed) != 1:
        return None
    recorded = failed[0]
    action_id = str(recorded.get("action_id") or "")
    original = next((result for result in results if result.action_id == action_id), None)
    recorded_steps = recorded.get("steps") or []
    if original is None or not isinstance(recorded_steps, list) or len(recorded_steps) != len(original.steps):
        return None
    statuses = [str(item.get("status") or "") if isinstance(item, dict) else "" for item in recorded_steps]
    try:
        failed_index = statuses.index("failed")
    except ValueError:
        return None
    completed_types = [step.step_type for step in original.steps[:failed_index]]
    remaining_types = [step.step_type for step in original.steps[failed_index:]]
    if (
        any(status != "ok" for status in statuses[:failed_index])
        or completed_types != ["quarantine_file_backend", "quarantine_file_gfid"]
        or remaining_types != ["restore_file_backend_gap_fill", "attach_file_gfid"]
        or any(status not in {"failed", "planned"} for status in statuses[failed_index:])
    ):
        return None
    continuation = copy.deepcopy(original)
    continuation.steps = copy.deepcopy(original.steps[failed_index:])
    for step in continuation.steps:
        step.status = "planned"
        step.returncode = None
        step.message = ""
        if step.step_type == "restore_file_backend_gap_fill":
            step.command_preview = rsync_brick_pull_command(
                step.host,
                step.source_host,
                step.source_path,
                step.target_path,
            )
            step.notes.append("transport command regenerated from the recorded source and target")
    continuation.notes.append(
        "post-preservation continuation: replay only the recorded failed copy and pending GFID attachment"
    )
    details = {
        "action_id": action_id,
        "completed_step_ids": [step.step_id for step in original.steps[:failed_index]],
        "remaining_step_ids": [step.step_id for step in continuation.steps],
        "reason": "recorded preserve-first steps completed; direct payload copy failed before GFID attachment",
    }
    return [continuation], details


def _existing_post_preservation_continuation(
    status: dict[str, Any],
    results: list[ApplyActionResult],
) -> tuple[list[ApplyActionResult], dict[str, Any]] | None:
    """Keep a bounded continuation retryable without reconstructing its prefix."""
    details = status.get("failed_execution_continuation")
    if not isinstance(details, dict) or len(results) != 1:
        return None
    result = results[0]
    if str(details.get("action_id") or "") != result.action_id:
        return None
    if [step.step_type for step in result.steps] != ["restore_file_backend_gap_fill", "attach_file_gfid"]:
        return None
    continuation = copy.deepcopy(results)
    copy_step = continuation[0].steps[0]
    copy_step.command_preview = rsync_brick_pull_command(
        copy_step.host,
        copy_step.source_host,
        copy_step.source_path,
        copy_step.target_path,
    )
    copy_step.notes.append("transport command regenerated from the recorded source and target")
    return continuation, dict(details)


def _post_copy_split_brain_marker_continuation(
    paths: Any,
    summary: dict[str, Any],
    results: list[ApplyActionResult],
    assistants: dict[str, Any],
) -> tuple[list[ApplyActionResult], dict[str, Any]] | None:
    """Create one reviewed resolver step after a verified preserve-first repair."""
    original_path = Path(paths.root) / "apply-before-post-preservation-continuation.json"
    if not original_path.is_file():
        return None
    originals = load_apply_results(original_path)
    if len(originals) != 1:
        return None
    original = originals[0]
    expected_types = [
        "quarantine_file_backend",
        "quarantine_file_gfid",
        "restore_file_backend_gap_fill",
        "attach_file_gfid",
    ]
    if [step.step_type for step in original.steps] != expected_types:
        return None

    report = _load_json(Path(paths.root) / "execute-results.json", {})
    completed = next(
        (
            item
            for item in report.get("actions") or []
            if isinstance(item, dict)
            and str(item.get("action_id") or "") == original.action_id
            and str(item.get("status") or "") in {"completed", "completed-with-nonblocking-skips"}
        ),
        None,
    )
    recorded_steps = completed.get("steps") if isinstance(completed, dict) else None
    if not isinstance(recorded_steps, list) or [
        str(item.get("step_type") or "") if isinstance(item, dict) else ""
        for item in recorded_steps
    ] != expected_types[2:]:
        return None
    if any(
        not isinstance(item, dict) or str(item.get("status") or "") != "ok"
        for item in recorded_steps
    ):
        return None

    current = next((result for result in results if result.logical_path == original.logical_path), None)
    assistant = _assistant_map(assistants).get(current.action_id if current else "", {})
    action = _choice_action(assistant)
    checksum = (assistant.get("details") or {}).get("checksum") or {}
    if (
        current is None
        or str(action.get("action_type") or "") != "review_entry_split_brain"
        or "heal_info_marks_split_brain" not in {str(note) for note in action.get("notes") or []}
        or str(checksum.get("outcome") or "") != "content-equal"
    ):
        return None

    source_host = original.steps[2].source_host
    source_backend = original.steps[2].source_path
    canonical_gfid = original.steps[3].source_path
    roles = {
        str(host): str(role).strip().lower()
        for host, role in (action.get("brick_roles_by_host") or {}).items()
        if str(host) and str(role)
    }
    brick_host_aliases = {
        str(host): [str(alias) for alias in aliases if str(alias)]
        for host, aliases in (action.get("brick_host_aliases") or {}).items()
        if str(host) and isinstance(aliases, list)
    }
    data_copies = [
        copy
        for copy in action.get("file_copies") or []
        if isinstance(copy, dict)
        and role_for_host(roles, str(copy.get("host") or ""), brick_host_aliases) == "data"
    ]
    current_identities = {
        str(copy.get("identity") or copy.get("backend_trusted_gfid") or copy.get("file_gfid") or "")
        for copy in data_copies
    }
    if (
        role_for_host(roles, source_host, brick_host_aliases) != "data"
        or len(data_copies) < 2
        or current_identities != {canonical_gfid}
    ):
        return None

    resolver_preview = _gluster_split_brain_preview(
        str(summary.get("volume") or ""),
        original.logical_path,
        winner_host=source_host,
        brick_path=str(summary.get("brick_path") or "") or _brick_root_from_backend(source_backend, original.logical_path),
        native_policy="source-brick",
    )
    if not resolver_preview:
        return None

    action_id = f"{original.action_id}:clear-split-brain-marker"
    continuation = ApplyActionResult(
        action_id=action_id,
        logical_path=original.logical_path,
        action_type="clear_split_brain_marker",
        execution_mode="dry-run",
        backup_root=original.backup_root,
        backup_mode=original.backup_mode,
        batch=False,
        status="planned",
        notes=[
            "repair strategy: clear_split_brain_marker",
            "post-copy marker continuation: the earlier evidence-authorized data source and canonical GFID are recorded in the preserved original plan",
            "fresh role evidence confirms the recorded source remains a data brick, and fresh checksums prove the data-brick copies now match",
            "the earlier named quarantines remain the rollback protection for the replaced conflict",
            "this final Gluster source-brick command only clears the remaining split-brain marker and restores mount visibility; it does not select a new source",
        ],
        brick_roles_by_host=roles,
        brick_host_aliases=brick_host_aliases,
        brick_role_evidence_required=bool(action.get("brick_role_evidence_required")),
        brick_role_evidence_error=str(action.get("brick_role_evidence_error") or ""),
    )
    continuation.steps.append(
        ApplyStep(
            step_id=_step_id(action_id, 1, "clear-split-brain-marker"),
            step_type="resolve_split_brain_gluster_cli",
            host=source_host,
            source_path=source_backend,
            target_path=original.logical_path,
            command_preview=resolver_preview,
            notes=[
                "official Gluster marker-clearing step after verified copy and canonical GFID attachment",
                "the recorded data-brick source was chosen earlier by matching data-plus-arbiter identity evidence; the arbiter is never a payload source",
                "fresh checksums confirm the data copies now match before this command runs",
                "if this resolver is inconclusive or fails, stop and rebuild evidence rather than selecting another source",
            ],
        )
    )
    details = {
        "original_action_id": original.action_id,
        "continuation_action_id": action_id,
        "source_host": source_host,
        "canonical_gfid": canonical_gfid,
        "reason": "verified source-copy repair completed; only Gluster split-brain marker clearing remains",
    }
    return [continuation], details


def _post_marker_alternate_source_continuation(
    paths: Any,
    summary: dict[str, Any],
    results: list[ApplyActionResult],
    assistants: dict[str, Any],
) -> tuple[list[ApplyActionResult], dict[str, Any]] | None:
    """Offer one alternate equal-content data source when Gluster retains a marker."""
    status = _load_json(Path(paths.status), {})
    prior = status.get("split_brain_marker_continuation")
    if not isinstance(prior, dict) or status.get("split_brain_marker_alternate_continuation"):
        return None
    prior_action_id = str(prior.get("continuation_action_id") or "")
    source_host = str(prior.get("source_host") or "")
    canonical_gfid = str(prior.get("canonical_gfid") or "")
    report = _load_json(Path(paths.root) / "execute-results.json", {})
    if not _recorded_source_brick_resolver_succeeded(report, prior_action_id) or not source_host or not canonical_gfid:
        return None

    current = next((result for result in results if result.logical_path == str(prior.get("logical_path") or result.logical_path)), None)
    if current is None:
        current = results[0] if len(results) == 1 else None
    assistant = _assistant_map(assistants).get(current.action_id if current else "", {})
    action = _choice_action(assistant)
    checksum = (assistant.get("details") or {}).get("checksum") or {}
    if (
        current is None
        or str(action.get("action_type") or "") != "review_entry_split_brain"
        or "heal_info_marks_split_brain" not in {str(note) for note in action.get("notes") or []}
        or str(checksum.get("outcome") or "") != "content-equal"
    ):
        return None

    roles = {
        str(host): str(role).strip().lower()
        for host, role in (action.get("brick_roles_by_host") or {}).items()
        if str(host) and str(role)
    }
    brick_host_aliases = {
        str(host): [str(alias) for alias in aliases if str(alias)]
        for host, aliases in (action.get("brick_host_aliases") or {}).items()
        if str(host) and isinstance(aliases, list)
    }
    candidates = [
        copy
        for copy in action.get("file_copies") or []
        if isinstance(copy, dict)
        and role_for_host(roles, str(copy.get("host") or ""), brick_host_aliases) == "data"
        and str(copy.get("host") or "") != source_host
        and str(copy.get("identity") or copy.get("backend_trusted_gfid") or copy.get("file_gfid") or "") == canonical_gfid
        and str(copy.get("backend") or "")
    ]
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    alternate_host = str(candidate.get("host") or "")
    alternate_backend = str(candidate.get("backend") or "")
    resolver_preview = _gluster_split_brain_preview(
        str(summary.get("volume") or ""),
        current.logical_path,
        winner_host=alternate_host,
        brick_path=_brick_root_from_backend(alternate_backend, current.logical_path),
        native_policy="source-brick",
    )
    if not resolver_preview:
        return None

    action_id = f"{prior_action_id}:alternate-data-source"
    continuation = ApplyActionResult(
        action_id=action_id,
        logical_path=current.logical_path,
        action_type="clear_split_brain_marker",
        execution_mode="dry-run",
        backup_root=str(getattr(current, "backup_root", "")),
        backup_mode=str(getattr(current, "backup_mode", "required")),
        batch=False,
        status="planned",
        notes=[
            "repair strategy: clear_split_brain_marker",
            "alternate-source marker continuation: the first official source-brick resolver completed, but fresh heal evidence still contains the canonical GFID",
            "fresh role evidence and matching checksums prove both data copies share the canonical identity and content",
            "the alternate data brick is used only to clear Gluster's remaining marker; it does not select different content or use the arbiter as a payload source",
            "this is a one-time fallback; if the marker remains afterward, stop and prepare a bounded support handoff",
        ],
        brick_roles_by_host=roles,
        brick_host_aliases=brick_host_aliases,
        brick_role_evidence_required=bool(action.get("brick_role_evidence_required")),
        brick_role_evidence_error=str(action.get("brick_role_evidence_error") or ""),
    )
    continuation.steps.append(
        ApplyStep(
            step_id=_step_id(action_id, 1, "clear-split-brain-marker-alternate-source"),
            step_type="resolve_split_brain_gluster_cli",
            host=alternate_host,
            source_path=alternate_backend,
            target_path=current.logical_path,
            command_preview=resolver_preview,
            notes=[
                "one-time official Gluster source-brick fallback after the first source completed without clearing the canonical marker",
                "fresh checksums prove this data source has identical content and the canonical GFID",
                "the arbiter is never a payload source",
                "if Gluster leaves the marker after this alternate source, stop and rebuild the support evidence rather than retrying another source",
            ],
        )
    )
    details = {
        "prior_action_id": prior_action_id,
        "continuation_action_id": action_id,
        "source_host": alternate_host,
        "canonical_gfid": canonical_gfid,
        "reason": "first source-brick resolver completed but fresh heal evidence retained the canonical GFID marker",
    }
    return [continuation], details


def _mark_persistent_split_brain_marker_for_support(
    paths: Any,
    results: list[ApplyActionResult],
    assistants: dict[str, Any],
) -> bool:
    """Turn exhausted equal-content resolver residue into a support-ready terminal card."""
    status = _load_json(Path(paths.status), {})
    alternate = status.get("split_brain_marker_alternate_continuation")
    if not isinstance(alternate, dict):
        return False
    alternate_action_id = str(alternate.get("continuation_action_id") or "")
    report = _load_json(Path(paths.root) / "execute-results.json", {})
    if not _recorded_source_brick_resolver_succeeded(report, alternate_action_id):
        return False
    assistant_by_id = _assistant_map(assistants)
    for result in results:
        assistant = assistant_by_id.get(result.action_id, {})
        action = _choice_action(assistant)
        checksum = (assistant.get("details") or {}).get("checksum") or {}
        if (
            str(action.get("action_type") or "") != "review_entry_split_brain"
            or "heal_info_marks_split_brain" not in {str(note) for note in action.get("notes") or []}
            or str(checksum.get("outcome") or "") != "content-equal"
        ):
            continue
        result.action_type = "review_persistent_split_brain_marker"
        result.status = "review"
        result.recommended_choice = "create_support_case"
        result.recommended_reason = (
            "Two checksum-equal data-brick source resolvers completed, but Gluster still reports the canonical GFID."
        )
        result.notes.extend(
            [
                "repair strategy: persistent_split_brain_marker_support",
                "the original and alternate data-brick source resolvers both completed after matching GFID and checksum evidence",
                "Gluster still reports the same canonical GFID; do not retry another source or quarantine matching copies",
                "recommended next step: create the bounded support handoff with this run's manifest, checksums, resolver commands, heal output, and named quarantine rollback paths",
            ]
        )
        return True
    return False


def _execute_ready_batch(
    paths: Any,
    summary: dict[str, Any],
    output_stream: TextIO,
) -> tuple[bool, dict[str, Any]]:
    """Delegate one explicitly approved batch to the canonical apply-run command."""
    from .cli import main as cli_main

    execution_dir = Path(paths.root) / "execution"
    results_out = Path(paths.root) / "execute-results.json"
    argv = [
        "apply-run",
        "--execute",
        "--execute-ready",
        "--apply-in",
        str(paths.apply),
        "--status-file",
        str(paths.status),
        "--run-dir",
        str(execution_dir),
        "--results-out",
        str(results_out),
        "--manage-heal",
        "--verify-temp-mount",
        "--verify-temp-mount-cleanup",
        "-y",
    ]
    _write_output(output_stream, "Running the canonical apply-run --execute --execute-ready path...")
    try:
        with contextlib.redirect_stdout(output_stream), contextlib.redirect_stderr(output_stream):
            exit_code = int(cli_main(argv) or 0)
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
    except Exception as exc:
        _write_output(output_stream, f"Guarded execution stopped before completion: {exc}")
        return False, {"exit_code": 2, "error": str(exc), "actions": [], "summary": {}, "write_outcome_unknown": True}
    if not results_out.is_file():
        report = {"exit_code": exit_code, "actions": [], "summary": {}, "write_outcome_unknown": True}
        if exit_code == 130:
            resume_hint = f"gluster-manager repair --status {paths.status} --granular"
            update_status(
                paths.status,
                phase="simple-interactive-execution-interrupted",
                execution_interrupted=True,
                execution_run_dir=str(execution_dir),
                execution_write_state="unknown",
                write_outcome_unknown=True,
                execution_resume=resume_hint,
            )
            report["interrupted"] = True
            _write_output(output_stream, "Execution interrupted; heal settings were restored, but the write state is unknown.")
            _write_output(output_stream, f"Inspect before resuming: {resume_hint}")
        else:
            _write_output(output_stream, f"Execution did not produce its report: {results_out}")
        return False, report
    report = _load_json(results_out, {})
    report["exit_code"] = exit_code
    execute_summary = report.get("summary") or {}
    failed_actions = int(execute_summary.get("failed_actions", 0) or 0)
    current_status = _load_json(Path(paths.status), {})
    final_guard = current_status.get("final_check_guard") or {}
    guard_failed = bool(final_guard.get("hit_limit") or final_guard.get("churn_hit_limit"))
    history = apply_write_history(
        {},
        current_status,
        {"execution_summary": execute_summary},
    )
    success = exit_code == 0 and failed_actions == 0 and not guard_failed
    update_status(
        paths.status,
        phase="simple-interactive-execution-complete" if success else "simple-interactive-execution-stopped",
        execution_out=str(results_out),
        execution_summary=execute_summary,
        execution_action_ids=sorted(_execution_action_ids(report)),
        **history,
        simple_execution={
            "exit_code": exit_code,
            "success": success,
            "results_out": str(results_out),
            "execution_dir": str(execution_dir),
            "stopped_by_final_check_guard": guard_failed,
        },
    )
    if not success:
        _write_output(output_stream, "Execution stopped; simple mode will not write another batch until the result and fresh evidence are reviewed.")
    return success, report


def run_simple_interaction(
    paths: Any,
    summary: dict[str, Any],
    results: list[ApplyActionResult],
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    resume: bool = False,
    allow_execution: bool = False,
    safe_auto: bool = False,
    safe_auto_authorized: bool = False,
    non_interactive_safe_auto: bool = False,
) -> int:
    previous = _load_json(Path(paths.interaction), {})
    assistants = _load_json(_path(paths, "assistants", "assistants.json"), {})
    current_status = _load_json(Path(paths.status), {})
    apply_write_history(summary, current_status, previous)
    executed_action_ids = {
        str(value)
        for value in current_status.get("execution_action_ids", [])
        if str(value)
    }
    completed_preservation_action_ids = _completed_preservation_action_ids(
        _load_json(Path(paths.root) / "execute-results.json", {})
    )
    cards, ready_batch = build_simple_decision_cards(
        results,
        assistants,
        executed_action_ids=executed_action_ids,
        completed_preservation_action_ids=completed_preservation_action_ids,
    )
    stale_choices = _invalidate_and_report_stale_choices(
        paths,
        cards,
        output_stream,
    )
    interaction = _card_payload(
        cards,
        ready_batch,
        previous=previous,
        write_occurred=bool(summary.get("write_occurred")),
    )
    _apply_interaction_write_history(interaction, summary, current_status, previous)
    if stale_choices:
        interaction["position"] = _first_stale_position(cards, stale_choices)
    write_json_shared(paths.interaction, interaction)
    _append_event(
        Path(paths.events),
        {
            "event": "interaction-start",
            "resume": resume,
            "decision_count": len(cards),
            "safe_auto": safe_auto,
            "safe_auto_authorized": safe_auto_authorized,
        },
    )

    while ready_batch and allow_execution:
        _render_ready_batch(ready_batch, output_stream)
        operator_approved_batch = any(
            _is_confirmed_operator_action(result) for result in ready_batch
        )
        if safe_auto and safe_auto_authorized and not operator_approved_batch:
            _write_output(output_stream, "Safe-auto authorization is active; executing this ready-safe batch.")
            batch_choice = "a"
        else:
            _write_output(
                output_stream,
                "Choice: [a] authorize this batch, [s] skip it (no changes), "
                "[d] defer it, [q] save and quit",
            )
            try:
                batch_choice = input_stream.readline()
            except KeyboardInterrupt:
                batch_choice = "q"
            batch_choice = batch_choice.strip().lower() if batch_choice else "q"
        if batch_choice == "a":
            if safe_auto:
                safe_auto_authorized = True
            success, execution_report = _execute_ready_batch(paths, summary, output_stream)
            if not success:
                interrupted = bool(execution_report.get("interrupted"))
                current_status = _load_json(Path(paths.status), {})
                history = apply_write_history(summary, current_status, execution_report)
                interaction["state"] = "paused"
                interaction["ready_batch_state"] = "execution-interrupted" if interrupted else "execution-stopped"
                _apply_interaction_write_history(interaction, summary, current_status, execution_report)
                write_json_shared(paths.interaction, interaction)
                write_json_shared(paths.root / "summary.json", summary)
                status_history = dict(history)
                status_history["execution_write_state"] = (
                    "unknown" if interrupted else history["execution_write_state"] or "failed"
                )
                update_status(
                    paths.status,
                    phase="simple-interactive-execution-interrupted" if interrupted else "simple-interactive-execution-stopped",
                    interaction=interaction,
                    pending_decision=None,
                    **status_history,
                )
                if interrupted:
                    _write_output(output_stream, "Do not retry the batch until the recorded execution artifacts have been inspected.")
                    _write_output(output_stream, f"Inspect before resuming: gluster-manager repair --status {paths.status} --granular")
                else:
                    _write_output(output_stream, f"Resume after reviewing the execution artifacts: gluster-manager repair --resume {paths.root}")
                return 2
            executed_action_ids.update(_execution_action_ids(execution_report))
            completed_preservation_action_ids.update(
                _completed_preservation_action_ids(execution_report)
            )
            write_history = apply_write_history(summary, current_status, execution_report)
            summary["execution_summary"] = execution_report.get("summary", {})
            summary["next_action"] = "review fresh evidence and the next simple-repair decision card"
            write_json_shared(paths.root / "summary.json", summary)
            _apply_interaction_write_history(interaction, summary, execution_report)
            write_json_shared(paths.interaction, interaction)
            _append_event(
                Path(paths.events),
                {
                    "event": "ready-batch-executed",
                    "action_ids": sorted(_execution_action_ids(execution_report)),
                },
            )
            try:
                results, assistants = _refresh_evidence(paths, summary)
            except Exception as exc:
                interaction["state"] = "paused"
                interaction["ready_batch_state"] = "evidence-refresh-failed"
                _apply_interaction_write_history(interaction, summary, execution_report)
                write_json_shared(paths.interaction, interaction)
                update_status(
                    paths.status,
                    phase="simple-interactive-evidence-refresh-blocked",
                    interaction=interaction,
                    **write_history,
                    evidence_refresh_error=str(exc),
                )
                _write_output(output_stream, f"Execution completed, but fresh evidence could not be collected: {exc}")
                _write_output(output_stream, f"Stop here and resume after inspection: gluster-manager repair --resume {paths.root}")
                return 2
            cards, ready_batch = build_simple_decision_cards(
                results,
                assistants,
                executed_action_ids=executed_action_ids,
                completed_preservation_action_ids=completed_preservation_action_ids,
            )
            stale_choices = _invalidate_and_report_stale_choices(
                paths,
                cards,
                output_stream,
            )
            interaction = _card_payload(
                cards,
                ready_batch,
                previous=interaction,
                write_occurred=bool(summary.get("write_occurred")),
            )
            if stale_choices:
                interaction["position"] = _first_stale_position(
                    cards,
                    stale_choices,
                )
            interaction["last_execution"] = execution_report.get("summary", {})
            write_json_shared(paths.interaction, interaction)
            _write_output(output_stream, "Batch completed; fresh evidence was collected and the plan was rebuilt.")
            continue
        if batch_choice == "s":
            ready_batch = []
            interaction["ready_batch_state"] = "skipped"
            write_json_shared(paths.interaction, interaction)
            _append_event(Path(paths.events), {"event": "ready-batch-skipped"})
            _write_output(output_stream, "Ready batch skipped; no changes were made.")
            break
        if batch_choice == "d":
            ready_batch = []
            interaction["ready_batch_state"] = "deferred"
            write_json_shared(paths.interaction, interaction)
            _append_event(Path(paths.events), {"event": "ready-batch-deferred"})
            _write_output(output_stream, "Ready-safe batch deferred; no changes were made for it.")
            break
        if batch_choice == "q":
            interaction["state"] = "paused"
            interaction["ready_batch_state"] = "pending"
            write_json_shared(paths.interaction, interaction)
            _append_event(Path(paths.events), {"event": "interaction-paused", "reason": "ready-batch"})
            update_status(
                paths.status,
                phase="simple-interactive-paused",
                interaction=interaction,
                pending_decision=None,
                write_occurred=bool(summary.get("write_occurred")),
            )
            _write_output(output_stream, f"Saved. Resume with: gluster-manager repair --resume {paths.root}")
            return 0
        _write_output(
            output_stream,
            "Choose a to authorize, s to skip without changes, d to defer, "
            "or q to save and quit.",
        )

    if ready_batch and not allow_execution:
        operator_approved_batch = any(
            _is_confirmed_operator_action(result) for result in ready_batch
        )
        batch_label = "Operator-approved" if operator_approved_batch else "Clear ready-safe"
        _write_output(
            output_stream,
            f"{batch_label} plan: {len(ready_batch)} action(s) grouped for one later batch confirmation.",
        )
    if cards and non_interactive_safe_auto:
        interaction["state"] = "paused"
        interaction["safe_auto_state"] = "stopped-at-decision"
        interaction["position"] = 0
        interaction["pending_decision"] = cards[0].decision_id
        write_json_shared(paths.interaction, interaction)
        write_text_shared(paths.support_summary, _support_summary(paths, summary, cards[0]))
        support_path = str(paths.support_summary)
        update_status(
            paths.status,
            phase="simple-safe-auto-stopped-at-decision",
            interaction=interaction,
            pending_decision=cards[0].to_dict(),
            safe_auto_state="stopped-at-decision",
            support_summary=support_path,
            write_occurred=bool(summary.get("write_occurred")),
        )
        _write_output(
            output_stream,
            "Safe-auto stopped before an unresolved operator decision; no guess was applied.",
        )
        _write_output(
            output_stream,
            f"Resume interactively: gluster-manager repair --resume {paths.root} --interactive",
        )
        if support_path:
            _write_output(output_stream, f"Support-case handoff: {support_path}")
        return 2
    if not cards:
        interaction["state"] = "clear"
        write_json_shared(paths.interaction, interaction)
        update_status(
            paths.status,
            phase="simple-interactive-clear",
            interaction=interaction,
            pending_decision=None,
            write_occurred=bool(summary.get("write_occurred")),
        )
        _write_output(output_stream, "No sticking points require operator input.")
        _write_output(
            output_stream,
            "Execution completed and no further sticking points remain."
            if summary.get("write_occurred")
            else "No changes were made in read-only simple mode.",
        )
        return 0

    _write_output(
        output_stream,
        (
            f"Simple repair found {len(cards)} sticking point(s); no additional changes will be made "
            "until each one is resolved."
            if summary.get("write_occurred")
            else f"Simple repair found {len(cards)} sticking point(s); no changes will be made in this stage."
        ),
    )
    position = int(interaction.get("position") or 0)
    while position < len(cards):
        card = cards[position]
        interaction["position"] = position
        interaction["pending_decision"] = card.decision_id
        write_json_shared(paths.interaction, interaction)
        update_status(
            paths.status,
            phase="simple-interactive-pending",
            interaction=interaction,
            pending_decision=card.to_dict(),
            write_occurred=bool(summary.get("write_occurred")),
        )
        _render_card(card, position + 1, len(cards), output_stream)
        output_stream.write("Choice: ")
        output_stream.flush()
        try:
            choice = input_stream.readline()
        except KeyboardInterrupt:
            choice = "q"
        if choice == "":
            choice = "q"
        choice = choice.strip().lower()
        if choice in {"?", "i"}:
            _render_detail(card, "?", output_stream)
            continue
        if choice in {"e", "p", "b"}:
            _render_detail(card, choice, output_stream)
            continue
        if choice == "t":
            try:
                _retry_health(paths, summary, output_stream)
            except Exception as exc:
                _write_output(output_stream, f"Health retry failed safely: {exc}")
            continue
        if choice == "x":
            try:
                results, assistants = _refresh_evidence(paths, summary)
                cards, ready_batch = build_simple_decision_cards(
                    results,
                    assistants,
                    executed_action_ids=executed_action_ids,
                    completed_preservation_action_ids=completed_preservation_action_ids,
                )
                stale_choices = _invalidate_and_report_stale_choices(
                    paths,
                    cards,
                    output_stream,
                )
                _write_output(output_stream, "Read-only evidence refreshed; the decision queue was rebuilt.")
                interaction = _card_payload(
                    cards,
                    ready_batch,
                    previous=interaction,
                    write_occurred=bool(summary.get("write_occurred")),
                )
                if stale_choices:
                    position = _first_stale_position(cards, stale_choices)
                    interaction["position"] = position
                write_json_shared(paths.interaction, interaction)
            except Exception as exc:
                _write_output(output_stream, f"Evidence refresh failed safely: {exc}")
            continue
        if choice == "n":
            try:
                results, assistants = _same_source_replan(paths, summary)
                cards, ready_batch = build_simple_decision_cards(
                    results,
                    assistants,
                    executed_action_ids=executed_action_ids,
                    completed_preservation_action_ids=completed_preservation_action_ids,
                )
                stale_choices = _invalidate_and_report_stale_choices(
                    paths,
                    cards,
                    output_stream,
                )
                _write_output(output_stream, "Read-only same-source replan complete; the decision queue was rebuilt.")
                interaction = _card_payload(
                    cards,
                    ready_batch,
                    previous=interaction,
                    write_occurred=bool(summary.get("write_occurred")),
                )
                if stale_choices:
                    position = _first_stale_position(cards, stale_choices)
                    interaction["position"] = position
                write_json_shared(paths.interaction, interaction)
            except Exception as exc:
                _write_output(output_stream, f"Same-source replan failed safely: {exc}")
            continue
        if choice == "g":
            _write_output(output_stream, _granular_handoff(paths))
            continue
        selected_spec = next(
            (
                spec
                for spec in card.available_choices
                if str(spec.get("key") or "") == choice
            ),
            None,
        )
        if selected_spec is not None:
            if not _confirm_operator_choice(
                card,
                selected_spec,
                input_stream=input_stream,
                output_stream=output_stream,
            ):
                continue
            decision_payload = dict(selected_spec.get("decision") or {})
            _save_decision(
                paths,
                card,
                f"guided-choice-{selected_spec['key']}",
                position=position,
                decision=decision_payload,
                choice_spec=selected_spec,
                confirmed=True,
            )
            _append_event(
                Path(paths.events),
                {
                    "event": "guided-decision",
                    "decision_id": card.decision_id,
                    "choice": selected_spec["label"],
                    "decision": decision_payload,
                    "confirmed": True,
                },
            )
            try:
                if selected_spec.get("risk_class") == "chain_follow":
                    results, assistants = _refresh_evidence(paths, summary)
                    refresh_message = (
                        "Native heal/rescan completed through the original evidence route; "
                        "the decision queue was rebuilt."
                    )
                else:
                    results, assistants = _same_source_replan(paths, summary)
                    refresh_message = (
                        "Decision recorded and the dry-run plan was rebuilt from the selected source/branch."
                    )
                cards, ready_batch = build_simple_decision_cards(
                    results,
                    assistants,
                    executed_action_ids=executed_action_ids,
                    completed_preservation_action_ids=completed_preservation_action_ids,
                )
                _invalidate_and_report_stale_choices(paths, cards, output_stream)
                position = 0
                interaction = _card_payload(
                    cards,
                    ready_batch,
                    previous={"position": position},
                    write_occurred=bool(summary.get("write_occurred")),
                )
                write_json_shared(paths.interaction, interaction)
                _write_output(
                    output_stream,
                    refresh_message,
                )
                if ready_batch and not allow_execution:
                    _write_output(
                        output_stream,
                        f"Decision saved. Resume its guarded execution with: gluster-manager repair --resume {paths.root} --interactive --execute",
                    )
                if ready_batch and allow_execution:
                    return run_simple_interaction(
                        paths,
                        summary,
                        results,
                        input_stream=input_stream,
                        output_stream=output_stream,
                        resume=True,
                        allow_execution=True,
                        safe_auto=safe_auto,
                        safe_auto_authorized=safe_auto_authorized,
                        non_interactive_safe_auto=non_interactive_safe_auto,
                    )
            except Exception as exc:
                _write_output(output_stream, f"Decision recorded, but the safe replan failed: {exc}")
            continue
        if choice == "s":
            _save_decision(paths, card, "skip", position=position + 1)
            _append_event(
                Path(paths.events),
                {"event": "decision", "decision_id": card.decision_id, "choice": "skip"},
            )
            position += 1
            continue
        if choice == "d":
            _save_decision(paths, card, "defer", position=position + 1)
            _append_event(
                Path(paths.events),
                {"event": "decision", "decision_id": card.decision_id, "choice": "defer"},
            )
            position += 1
            continue
        if choice == "r":
            _save_decision(paths, card, "record-recommendation", position=position + 1)
            _append_event(
                Path(paths.events),
                {
                    "event": "decision",
                    "decision_id": card.decision_id,
                    "choice": "record-recommendation",
                },
            )
            position += 1
            continue
        if choice == "h":
            handoff = _support_summary(paths, summary, card)
            write_text_shared(paths.support_summary, handoff)
            _save_decision(paths, card, "support-case", position=position + 1)
            _append_event(
                Path(paths.events),
                {"event": "support-handoff", "decision_id": card.decision_id},
            )
            _write_output(output_stream, handoff)
            position += 1
            continue
        if choice == "q":
            interaction["position"] = position
            interaction["state"] = "paused"
            write_json_shared(paths.interaction, interaction)
            _append_event(
                Path(paths.events),
                {"event": "interaction-paused", "position": position},
            )
            update_status(
                paths.status,
                phase="simple-interactive-paused",
                interaction=interaction,
                pending_decision=card.to_dict(),
                write_occurred=bool(summary.get("write_occurred")),
            )
            _write_output(output_stream, f"Saved. Resume with: gluster-manager repair --resume {paths.root}")
            return 0
        _write_output(
            output_stream,
            "Choose a displayed supported choice or e, p, b, t, x, n, r, s, d, h, or q. No default was applied.",
        )

    interaction["position"] = len(cards)
    interaction["state"] = "complete"
    interaction.pop("pending_decision", None)
    write_json_shared(paths.interaction, interaction)
    _append_event(
        Path(paths.events),
        {"event": "interaction-complete", "decisions": len(cards)},
    )
    update_status(
        paths.status,
        phase="simple-interactive-complete",
        interaction=interaction,
        pending_decision=None,
        write_occurred=bool(summary.get("write_occurred")),
    )
    _write_output(
        output_stream,
        "All sticking points were skipped, deferred, or recorded for later review.",
    )
    _write_output(
        output_stream,
        "The guarded batch completed; remaining sticking points were deferred or recorded."
        if summary.get("write_occurred")
        else "No changes were made in read-only simple mode.",
    )
    return 0


def resume_simple_interaction(
    paths: Any,
    summary: dict[str, Any],
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    allow_execution: bool = False,
    safe_auto: bool = False,
    safe_auto_authorized: bool = False,
    non_interactive_safe_auto: bool = False,
) -> int:
    results = load_apply_results(paths.apply)
    status = _load_json(Path(paths.status), {})
    if (
        str(status.get("execution_write_state") or "") == "marker-continuation-pending"
        and len(results) == 1
        and results[0].action_type == "clear_split_brain_marker"
    ):
        return run_simple_interaction(
            paths,
            summary,
            results,
            input_stream=input_stream,
            output_stream=output_stream,
            resume=True,
            allow_execution=allow_execution,
            safe_auto=safe_auto,
            safe_auto_authorized=safe_auto_authorized,
            non_interactive_safe_auto=non_interactive_safe_auto,
        )
    if str(status.get("execution_write_state") or "") == "failed":
        continuation = _existing_post_preservation_continuation(status, results)
        if continuation is None:
            continuation = _post_preservation_file_continuation(paths, results)
        if continuation is not None:
            continuation_results, details = continuation
            original_apply = Path(paths.root) / "apply-before-post-preservation-continuation.json"
            if not original_apply.exists():
                original_apply.write_text(Path(paths.apply).read_text(encoding="utf-8"), encoding="utf-8")
            write_apply_results(
                paths.apply,
                continuation_results,
                decision_file=str(paths.decisions),
                controller_cycle={},
                previous_payload=load_plan(paths.apply),
            )
            update_status(
                paths.status,
                phase="simple-interactive-post-preservation-continuation",
                execution_action_ids=[],
                execution_write_state="continuation-pending",
                failed_execution_continuation=details,
                write_occurred=True,
            )
            _write_output(
                output_stream,
                "Resume found a bounded post-preservation continuation. It will not infer a new source: "
                "only the recorded failed copy and pending GFID attachment remain for review.",
            )
            return run_simple_interaction(
                paths,
                summary,
                continuation_results,
                input_stream=input_stream,
                output_stream=output_stream,
                resume=True,
                allow_execution=allow_execution,
                safe_auto=safe_auto,
                safe_auto_authorized=safe_auto_authorized,
                non_interactive_safe_auto=non_interactive_safe_auto,
            )
    if summary.get("evidence_inputs"):
        try:
            results, assistants = _refresh_evidence(paths, summary)
            marker_continuation = _post_copy_split_brain_marker_continuation(
                paths,
                summary,
                results,
                assistants,
            )
            if marker_continuation is None:
                marker_continuation = _post_marker_alternate_source_continuation(
                    paths,
                    summary,
                    results,
                    assistants,
                )
            if marker_continuation is not None:
                continuation_results, details = marker_continuation
                alternate_source = "prior_action_id" in details
                original_apply = Path(paths.root) / (
                    "apply-before-post-marker-alternate-source-continuation.json"
                    if alternate_source
                    else "apply-before-post-copy-marker-continuation.json"
                )
                if not original_apply.exists():
                    original_apply.write_text(Path(paths.apply).read_text(encoding="utf-8"), encoding="utf-8")
                write_apply_results(
                    paths.apply,
                    continuation_results,
                    decision_file=str(paths.decisions),
                    controller_cycle={},
                    previous_payload=load_plan(paths.apply),
                )
                update_status(
                    paths.status,
                    phase="simple-interactive-post-copy-marker-continuation",
                    execution_action_ids=[],
                    execution_write_state="marker-continuation-pending",
                    **(
                        {"split_brain_marker_alternate_continuation": details}
                        if alternate_source
                        else {"split_brain_marker_continuation": details}
                    ),
                    write_occurred=True,
                )
                _write_output(
                    output_stream,
                    (
                        "Fresh evidence still contains the canonical marker after the first resolver. One alternate, "
                        "checksum-equal data-source step remains for separate review."
                        if alternate_source
                        else "Fresh evidence verified the completed source-copy repair. One separately reviewed Gluster "
                        "marker-clearing step remains; it will not select a new source."
                    ),
                )
                return run_simple_interaction(
                    paths,
                    summary,
                    continuation_results,
                    input_stream=input_stream,
                    output_stream=output_stream,
                    resume=True,
                    allow_execution=allow_execution,
                    safe_auto=safe_auto,
                    safe_auto_authorized=safe_auto_authorized,
                    non_interactive_safe_auto=non_interactive_safe_auto,
                )
            if _mark_persistent_split_brain_marker_for_support(paths, results, assistants):
                write_apply_results(
                    paths.apply,
                    results,
                    decision_file=str(paths.decisions),
                    controller_cycle={},
                    previous_payload=load_plan(paths.apply),
                )
                update_status(
                    paths.status,
                    phase="simple-interactive-persistent-split-brain-marker",
                    execution_write_state="support-handoff-recommended",
                    write_occurred=True,
                )
        except Exception as exc:
            update_status(
                paths.status,
                phase="simple-interactive-resume-refresh-blocked",
                evidence_refresh_error=str(exc),
                write_occurred=bool(summary.get("write_occurred")),
            )
            _write_output(
                output_stream,
                f"Resume stopped because fresh evidence could not be collected: {exc}",
            )
            _write_output(
                output_stream,
                "The saved decision was not executed; resolve the evidence error and resume again.",
            )
            return 2
        _write_output(
            output_stream,
            "Resume refreshed the original read-only evidence route before loading saved choices.",
        )
    return run_simple_interaction(
        paths,
        summary,
        results,
        input_stream=input_stream,
        output_stream=output_stream,
        resume=True,
        allow_execution=allow_execution,
        safe_auto=safe_auto,
        safe_auto_authorized=safe_auto_authorized,
        non_interactive_safe_auto=non_interactive_safe_auto,
    )
