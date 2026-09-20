# SPDX-License-Identifier: GPL-2.0-only
"""Directory tie comparison helpers."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
import json
import time
from pathlib import Path

from .models import ManifestObject, ResolutionObservation
from .shared_io import write_json_shared
from .canary_shared import _format_brick_roles_by_host_for_notes

DEFAULT_DIRECTORY_TIE_MATRIX_ROWS = 6
DEFAULT_DIRECTORY_TIE_BUDGET = (3, 1024, 4096)
HARD_DIRECTORY_TIE_BUDGET = (8, 8192, 65536)
DEFAULT_DIRECTORY_TIE_MEMINFO = "/proc/meminfo"


@dataclass
class DirectoryTieClassification:
    logical_path: str
    branch_type: str
    reason: str
    present_hosts: list[str] = field(default_factory=list)
    missing_hosts: list[str] = field(default_factory=list)
    child_gap_hosts: list[str] = field(default_factory=list)
    unique_child_names: list[str] = field(default_factory=list)
    host_child_counts: dict[str, int] = field(default_factory=dict)
    metadata_mismatch_hosts: list[str] = field(default_factory=list)
    quorum_threshold: int = 0


@dataclass
class DirectoryTieBudget:
    logical_path: str
    max_depth: int
    max_children_per_dir: int
    max_total_nodes: int
    unique_child_count: int
    max_host_child_count: int
    estimated_total_nodes: int
    over_budget: bool
    stop_reason: str


@dataclass
class DirectoryTieDiff:
    classification: DirectoryTieClassification
    budget: DirectoryTieBudget
    planner_hint: str
    next_depth_matches: bool = False
    next_depth_signature: list[str] = field(default_factory=list)
    diff_lines: list[str] = field(default_factory=list)


def read_mem_available_kib(path: str = DEFAULT_DIRECTORY_TIE_MEMINFO) -> int | None:
    try:
        for line in Path(path).read_text().splitlines():
            if not line.startswith("MemAvailable:"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    except OSError:
        return None
    return None


def resolve_directory_tie_budget(
    max_depth: int | None = None,
    max_children_per_dir: int | None = None,
    max_total_nodes: int | None = None,
    *,
    mem_available_kib: int | None = None,
) -> tuple[int, int, int]:
    if max_depth is not None and max_children_per_dir is not None and max_total_nodes is not None:
        return normalize_directory_tie_budget(max_depth, max_children_per_dir, max_total_nodes)
    if mem_available_kib is None:
        mem_available_kib = read_mem_available_kib()
    if mem_available_kib is None:
        return normalize_directory_tie_budget(
            max_depth or DEFAULT_DIRECTORY_TIE_BUDGET[0],
            max_children_per_dir or DEFAULT_DIRECTORY_TIE_BUDGET[1],
            max_total_nodes or DEFAULT_DIRECTORY_TIE_BUDGET[2],
        )

    resolved_depth = max_depth
    resolved_children = max_children_per_dir
    resolved_total = max_total_nodes

    if resolved_depth is None:
        if mem_available_kib >= 2 * 1024 * 1024:
            resolved_depth = DEFAULT_DIRECTORY_TIE_BUDGET[0]
        elif mem_available_kib >= 512 * 1024:
            resolved_depth = 2
        else:
            resolved_depth = 1
    if resolved_children is None:
        resolved_children = min(
            DEFAULT_DIRECTORY_TIE_BUDGET[1],
            max(64, mem_available_kib // 8192),
        )
    if resolved_total is None:
        resolved_total = min(
            DEFAULT_DIRECTORY_TIE_BUDGET[2],
            max(256, mem_available_kib // 2048),
        )
    return normalize_directory_tie_budget(
        resolved_depth,
        resolved_children,
        resolved_total,
    )


def _directory_present(obs: ResolutionObservation) -> bool:
    return obs.backend_exists and obs.backend_lstat_type == "dir"


def _directory_hosts(obj: ManifestObject) -> list[str]:
    return sorted(obj.observations.keys())


def _directory_child_names_by_host(obj: ManifestObject) -> dict[str, list[str]]:
    by_host: dict[str, list[str]] = {}
    for host, observations in sorted(obj.observations.items()):
        names: list[str] = []
        seen: set[str] = set()
        for obs in observations:
            for child_name in obs.backend_child_names:
                if child_name and child_name not in seen:
                    seen.add(child_name)
                    names.append(child_name)
        if not names:
            for child_path in obj.children:
                child_name = child_path.rsplit("/", 1)[-1].strip()
                if child_name and child_name not in seen:
                    seen.add(child_name)
                    names.append(child_name)
        if not names:
            for child_name in obj.child_names:
                if child_name and child_name not in seen:
                    seen.add(child_name)
                    names.append(child_name)
        by_host[host] = sorted(names)
    return by_host


def _shared_child_signature(child_names_by_host: dict[str, list[str]]) -> tuple[bool, list[str]]:
    signatures = {
        tuple(sorted(name for name in names if name))
        for names in child_names_by_host.values()
        if names
    }
    if len(signatures) != 1:
        return False, []
    signature = sorted(next(iter(signatures)))
    if len(signature) == 0:
        return False, []
    return True, signature


def _directory_has_gfid_conflict(obj: ManifestObject) -> bool:
    if "dir_name_gfid_conflict" in obj.notes:
        return True
    directory_gfids = sorted(
        {
            obs.backend_trusted_gfid or obs.gfid
            for observations in obj.observations.values()
            for obs in observations
            if obs.backend_lstat_type == "dir" and (obs.backend_trusted_gfid or obs.gfid)
        }
    )
    return len(directory_gfids) > 1 or (
        len(obj.gfids) > 1 and obj.object_type in {"directory", "directory_candidate"}
    )


def _directory_canonical_gfid(obj: ManifestObject) -> str:
    copies: list[tuple[str, int, int, str]] = []
    for host, observations in sorted(obj.observations.items()):
        for obs in observations:
            if not _directory_present(obs):
                continue
            identity = obs.backend_trusted_gfid or obs.gfid or ""
            if not identity:
                continue
            copies.append((identity, obs.backend_mtime or -1, obs.backend_size or -1, host))
    if not copies:
        return ""
    identity_counts = Counter(identity for identity, _mtime, _size, _host in copies)
    return sorted(
        copies,
        key=lambda item: (
            identity_counts.get(item[0], 0),
            item[1],
            item[2],
            item[3],
        ),
        reverse=True,
    )[0][0]


def _directory_metadata_mismatch_hosts(obj: ManifestObject, canonical_gfid: str) -> list[str]:
    mismatch_hosts: list[str] = []
    if not canonical_gfid:
        return mismatch_hosts
    for host, observations in sorted(obj.observations.items()):
        for obs in observations:
            if not _directory_present(obs):
                continue
            if obs.backend_trusted_gfid != canonical_gfid:
                mismatch_hosts.append(host)
                break
    return mismatch_hosts


def classify_directory_tie(obj: ManifestObject, *, quorum_count: int = 2) -> DirectoryTieClassification:
    present_hosts = [
        host
        for host, observations in sorted(obj.observations.items())
        if any(_directory_present(obs) for obs in observations)
    ]
    missing_hosts = [
        host
        for host, observations in sorted(obj.observations.items())
        if not any(_directory_present(obs) for obs in observations)
    ]
    child_names_by_host = _directory_child_names_by_host(obj)
    unique_child_names = sorted({name for names in child_names_by_host.values() for name in names if name})
    reference_child_names = set(max(child_names_by_host.values(), key=len, default=[]))
    child_gap_hosts = [
        host
        for host, names in child_names_by_host.items()
        if set(names) != reference_child_names
    ]
    host_child_counts = {host: len(names) for host, names in child_names_by_host.items()}
    canonical_gfid = _directory_canonical_gfid(obj)
    metadata_mismatch_hosts = _directory_metadata_mismatch_hosts(obj, canonical_gfid)
    quorum_threshold = max(1, min(quorum_count, len(obj.observations) or 1))
    present_count = len(present_hosts)

    if _directory_has_gfid_conflict(obj):
        branch_type = "gfid_conflict"
        reason = "same logical directory name appears with multiple GFIDs"
    elif child_gap_hosts and present_count >= quorum_threshold:
        branch_type = "child_gap"
        reason = "immediate child sets differ across replicas"
    elif metadata_mismatch_hosts and not missing_hosts:
        branch_type = "metadata_only"
        reason = "directory exists everywhere, but metadata differs across bricks"
    elif 0 < present_count < quorum_threshold:
        branch_type = "below_quorum"
        reason = f"directory is present on only {present_count} of {len(obj.observations) or 1} replicas; below quorum threshold {quorum_threshold}"
    elif present_count == 0 and obj.object_type in {"directory", "directory_candidate"}:
        branch_type = "missing"
        reason = "no brick reports a present directory copy"
    else:
        branch_type = "stable"
        reason = "directory is already consistent"

    return DirectoryTieClassification(
        logical_path=obj.logical_path,
        branch_type=branch_type,
        reason=reason,
        present_hosts=present_hosts,
        missing_hosts=missing_hosts,
        child_gap_hosts=child_gap_hosts,
        unique_child_names=unique_child_names,
        host_child_counts=host_child_counts,
        metadata_mismatch_hosts=metadata_mismatch_hosts,
        quorum_threshold=quorum_threshold,
    )


def sniff_subtree_size(
    obj: ManifestObject,
    *,
    max_depth: int = 3,
    max_children_per_dir: int = 1024,
    max_total_nodes: int = 4096,
) -> DirectoryTieBudget:
    max_depth, max_children_per_dir, max_total_nodes = normalize_directory_tie_budget(
        max_depth,
        max_children_per_dir,
        max_total_nodes,
    )
    child_names_by_host = _directory_child_names_by_host(obj)
    unique_child_names = sorted({name for names in child_names_by_host.values() for name in names if name})
    max_host_child_count = max((len(names) for names in child_names_by_host.values()), default=0)
    estimated_total_nodes = len(unique_child_names) + max_host_child_count
    over_budget = False
    stop_reason = ""
    if max_depth <= 0:
        over_budget = True
        stop_reason = "max_depth must be greater than zero"
    elif obj.depth > max_depth:
        over_budget = True
        stop_reason = f"directory depth {obj.depth} exceeds max_depth={max_depth}"
    elif len(unique_child_names) > max_children_per_dir:
        over_budget = True
        stop_reason = (
            f"immediate child count {len(unique_child_names)} exceeds max_children_per_dir={max_children_per_dir}"
        )
    elif estimated_total_nodes > max_total_nodes:
        over_budget = True
        stop_reason = f"estimated subtree size {estimated_total_nodes} exceeds max_total_nodes={max_total_nodes}"
    return DirectoryTieBudget(
        logical_path=obj.logical_path,
        max_depth=max_depth,
        max_children_per_dir=max_children_per_dir,
        max_total_nodes=max_total_nodes,
        unique_child_count=len(unique_child_names),
        max_host_child_count=max_host_child_count,
        estimated_total_nodes=estimated_total_nodes,
        over_budget=over_budget,
        stop_reason=stop_reason,
    )


def choose_directory_tie_action(
    classification: DirectoryTieClassification,
    budget: DirectoryTieBudget,
) -> str:
    if budget.over_budget:
        if classification.branch_type == "child_gap":
            return "review_directory_children"
        return "review_directory_metadata"
    if classification.branch_type == "gfid_conflict":
        return "review_directory_gfid_conflict"
    if classification.branch_type == "metadata_only":
        return "repair_directory_metadata"
    if classification.branch_type == "child_gap":
        return "reconcile_directory_children"
    if classification.branch_type == "below_quorum":
        return "review_directory_metadata"
    if classification.branch_type == "missing":
        return "review_directory_metadata"
    return "review_directory_metadata"


def bounded_tree_diff(
    obj: ManifestObject,
    *,
    quorum_count: int = 2,
    max_depth: int = 3,
    max_children_per_dir: int = 1024,
    max_total_nodes: int = 4096,
) -> DirectoryTieDiff:
    max_depth, max_children_per_dir, max_total_nodes = normalize_directory_tie_budget(
        max_depth,
        max_children_per_dir,
        max_total_nodes,
    )
    classification = classify_directory_tie(obj, quorum_count=quorum_count)
    child_names_by_host = _directory_child_names_by_host(obj)
    budget = sniff_subtree_size(
        obj,
        max_depth=max_depth,
        max_children_per_dir=max_children_per_dir,
        max_total_nodes=max_total_nodes,
    )
    next_depth_matches, next_depth_signature = _shared_child_signature(child_names_by_host)
    planner_hint = choose_directory_tie_action(classification, budget)
    diff_lines = [
        f"branch_type={classification.branch_type}",
        f"present_hosts={', '.join(classification.present_hosts) or 'none'}",
        f"missing_hosts={', '.join(classification.missing_hosts) or 'none'}",
        f"child_gap_hosts={', '.join(classification.child_gap_hosts) or 'none'}",
        f"unique_child_names={classification.unique_child_names and len(classification.unique_child_names) or 0}",
        f"next_depth_matches={next_depth_matches}",
        f"next_depth_signature={', '.join(next_depth_signature) or 'none'}",
        f"budget=depth:{budget.max_depth}, children:{budget.max_children_per_dir}, total:{budget.max_total_nodes}",
    ]
    if budget.over_budget:
        diff_lines.append(f"budget_stop={budget.stop_reason}")
    if classification.reason:
        diff_lines.append(f"reason={classification.reason}")
    return DirectoryTieDiff(
        classification=classification,
        budget=budget,
        planner_hint=planner_hint,
        next_depth_matches=next_depth_matches,
        next_depth_signature=next_depth_signature,
        diff_lines=diff_lines,
    )


def render_directory_tie_diff(diff: DirectoryTieDiff) -> str:
    return "\n".join(diff.diff_lines + [f"planner_hint={diff.planner_hint}"])


def _directory_action_child_names_by_host(action: dict[str, object]) -> dict[str, list[str]]:
    by_host: dict[str, list[str]] = {}
    raw_by_host = action.get("directory_child_names_by_host") or {}
    if isinstance(raw_by_host, dict):
        for host, names in sorted(raw_by_host.items()):
            host_name = str(host)
            child_names = sorted({str(name) for name in names or [] if str(name).strip()})
            if child_names:
                by_host[host_name] = child_names
    return by_host


def _directory_action_classification(action: dict[str, object]) -> DirectoryTieClassification:
    logical_path = str(action.get("logical_path") or "")
    action_type = str(action.get("action_type") or "")
    present_hosts = sorted({str(host) for host in action.get("healthy_hosts") or [] if str(host).strip()})
    missing_hosts = sorted({str(host) for host in action.get("missing_hosts") or [] if str(host).strip()})
    child_names_by_host = _directory_action_child_names_by_host(action)
    child_gap_hosts = sorted(
        host
        for host, names in (action.get("missing_directory_children_by_host") or {}).items()
        if names and str(host).strip()
    )
    unique_child_names = sorted({name for names in child_names_by_host.values() for name in names if name})
    host_child_counts = {host: len(names) for host, names in child_names_by_host.items()}
    metadata_mismatch_hosts = sorted(
        {
            str(host)
            for host in action.get("directory_metadata_mismatch_hosts") or []
            if str(host).strip()
        }
    )
    total_hosts = max(len(present_hosts) + len(missing_hosts), len(host_child_counts), 1)
    quorum_threshold = max(1, (total_hosts // 2) + 1)
    present_count = len(present_hosts)

    if action_type == "review_directory_gfid_conflict" or str(action.get("repair_strategy") or "") in {"rename_conflicting_directory", "quarantine_conflicting_directory"}:
        branch_type = "gfid_conflict"
        reason = "same logical directory name appears with multiple GFIDs"
    elif action_type == "review_directory_children" or str(action.get("repair_strategy") or "") == "reconcile_directory_children":
        branch_type = "child_gap"
        reason = "immediate child sets differ across replicas"
    elif action_type == "review_directory_metadata":
        if metadata_mismatch_hosts and not missing_hosts:
            branch_type = "metadata_only"
            reason = "directory exists everywhere, but metadata differs across bricks"
        elif present_count == 0:
            branch_type = "missing"
            reason = "no brick reports a present directory copy"
        elif 0 < present_count < quorum_threshold:
            branch_type = "below_quorum"
            reason = (
                f"directory is present on only {present_count} of {total_hosts} replicas; "
                f"below quorum threshold {quorum_threshold}"
            )
        else:
            branch_type = "metadata_only"
            reason = "directory metadata review remains after structural checks"
    elif present_count == 0:
        branch_type = "missing"
        reason = "no brick reports a present directory copy"
    elif 0 < present_count < quorum_threshold:
        branch_type = "below_quorum"
        reason = (
            f"directory is present on only {present_count} of {total_hosts} replicas; "
            f"below quorum threshold {quorum_threshold}"
        )
    else:
        branch_type = "stable"
        reason = "directory is already consistent"

    return DirectoryTieClassification(
        logical_path=logical_path,
        branch_type=branch_type,
        reason=reason,
        present_hosts=present_hosts,
        missing_hosts=missing_hosts,
        child_gap_hosts=child_gap_hosts,
        unique_child_names=unique_child_names,
        host_child_counts=host_child_counts,
        metadata_mismatch_hosts=metadata_mismatch_hosts,
        quorum_threshold=quorum_threshold,
    )


def _directory_action_budget(
    action: dict[str, object],
    *,
    max_depth: int = 3,
    max_children_per_dir: int = 1024,
    max_total_nodes: int = 4096,
) -> DirectoryTieBudget:
    child_names_by_host = _directory_action_child_names_by_host(action)
    unique_child_names = sorted({name for names in child_names_by_host.values() for name in names if name})
    max_host_child_count = max((len(names) for names in child_names_by_host.values()), default=0)
    depth = int(action.get("depth") or 0)
    estimated_total_nodes = len(unique_child_names) + max_host_child_count
    over_budget = False
    stop_reason = ""
    if max_depth <= 0:
        over_budget = True
        stop_reason = "max_depth must be greater than zero"
    elif depth > max_depth:
        over_budget = True
        stop_reason = f"directory depth {depth} exceeds max_depth={max_depth}"
    elif len(unique_child_names) > max_children_per_dir:
        over_budget = True
        stop_reason = (
            f"immediate child count {len(unique_child_names)} exceeds max_children_per_dir={max_children_per_dir}"
        )
    elif estimated_total_nodes > max_total_nodes:
        over_budget = True
        stop_reason = f"estimated subtree size {estimated_total_nodes} exceeds max_total_nodes={max_total_nodes}"
    return DirectoryTieBudget(
        logical_path=str(action.get("logical_path") or ""),
        max_depth=max_depth,
        max_children_per_dir=max_children_per_dir,
        max_total_nodes=max_total_nodes,
        unique_child_count=len(unique_child_names),
        max_host_child_count=max_host_child_count,
        estimated_total_nodes=estimated_total_nodes,
        over_budget=over_budget,
        stop_reason=stop_reason,
    )


def _directory_tie_choices(branch_type: str) -> list[str]:
    if branch_type == "gfid_conflict":
        return [
            "auto",
            "merge",
            "quarantine_both",
            "quarantine_loser",
            "keep_left",
            "keep_right",
            "prune_left",
            "prune_right",
            "defer",
        ]
    if branch_type == "child_gap":
        return ["auto", "reconcile_directory_children", "keep_review", "defer"]
    if branch_type == "metadata_only":
        return ["auto", "repair_directory_metadata", "keep_review", "defer"]
    if branch_type in {"below_quorum", "missing"}:
        return ["auto", "prune_left", "prune_right", "defer"]
    return ["auto", "defer"]


def _directory_tie_matrix_row(
    *,
    stage: str,
    condition: str,
    action: str,
    next_step: str,
    why: str,
    risk: str,
) -> dict[str, str]:
    return {
        "stage": stage,
        "if": condition,
        "then": action,
        "next": next_step,
        "why": why,
        "risk": risk,
    }


def _directory_tie_decision_matrix(diff: DirectoryTieDiff) -> list[dict[str, str]]:
    branch_type = diff.classification.branch_type
    matrix: list[dict[str, str]] = []
    if diff.budget.over_budget:
        matrix.append(
            _directory_tie_matrix_row(
                stage="budget",
                condition="budget exceeded",
                action="review_only",
                next_step="rerun with a tighter budget or switch to interactive review",
                why=diff.budget.stop_reason,
                risk="hard stop",
            )
        )
        return matrix
    if branch_type == "gfid_conflict" and diff.next_depth_matches:
        matrix.append(
            _directory_tie_matrix_row(
                stage="early",
                condition="next-depth metadata matches across the competing branches",
                action="collapse_shared_subtree",
                next_step="prune deeper tree comparison and keep the shared subtree",
                why="the next depth does not distinguish the branches, so the deeper walk can stop early",
                risk="safe_default",
            )
        )
    if branch_type == "gfid_conflict":
        if diff.classification.child_gap_hosts:
            matrix.append(
                _directory_tie_matrix_row(
                    stage="structure",
                    condition="child-gap is present",
                    action="reconcile_directory_children",
                    next_step="rerun directory-tie-build after the child set is consistent",
                    why="child disagreement should be resolved before tree comparison decisions",
                    risk="safe_default",
                )
            )
        if diff.classification.metadata_mismatch_hosts and not diff.classification.missing_hosts:
            matrix.append(
                _directory_tie_matrix_row(
                    stage="metadata",
                    condition="metadata drift is also present",
                    action="repair_directory_metadata",
                    next_step="rerun directory-tie-build to confirm the remaining tie shape",
                    why="xattr drift can be fixed independently from the tree conflict",
                    risk="safe_default",
                )
            )
        matrix.append(
            _directory_tie_matrix_row(
                stage="operator",
                condition="pure GFID/name conflict remains",
                action="merge / quarantine_both / quarantine_loser / keep_left / keep_right / prune_left / prune_right / defer",
                next_step="use a decision file or interactive confirmation to choose the branch; quarantine_* is the safer preserve-first path",
                why="the remaining tie is still ambiguous after bounded comparison and may still hold real data on both sides",
                risk="operator_choice",
            )
        )
        return matrix
    if branch_type == "child_gap":
        matrix.append(
            _directory_tie_matrix_row(
                stage="structure",
                condition="child dependency closure is executable",
                action="reconcile_directory_children",
                next_step="rerun manifest-build and plan-build so the parent can be reconsidered",
                why="the parent can only be trusted after the child set is consistent",
                risk="safe_default",
            )
        )
        matrix.append(
            _directory_tie_matrix_row(
                stage="review",
                condition="child gap is still ambiguous",
                action="review_directory_children",
                next_step="show the child diff and wait for a narrower canary or a fresh snapshot",
                why="the current child comparison is not yet safe enough to auto-promote",
                risk="review",
            )
        )
        return matrix
    if branch_type == "metadata_only":
        matrix.append(
            _directory_tie_matrix_row(
                stage="metadata",
                condition="directory exists everywhere and only xattr drift remains",
                action="repair_directory_metadata",
                next_step="apply the canonical directory GFID to the mismatched bricks",
                why="this is the safe metadata-only branch",
                risk="safe_default",
            )
        )
        matrix.append(
            _directory_tie_matrix_row(
                stage="review",
                condition="structure is still unresolved",
                action="review_directory_metadata",
                next_step="repair the child chain first, then rerun the scan",
                why="the directory candidate is still inferred from the children",
                risk="review",
            )
        )
        return matrix
    if branch_type in {"below_quorum", "missing"}:
        matrix.append(
            _directory_tie_matrix_row(
                stage="cleanup",
                condition="one side is clearly residue or below quorum",
                action="prune_left / prune_right / defer",
                next_step="choose the side that is stale residue, or defer if both sides still matter",
                why="the directory is not yet a safe merge candidate",
                risk="risky_default",
            )
        )
        return matrix
    matrix.append(
        _directory_tie_matrix_row(
            stage="terminal",
            condition="directory is already consistent",
            action="defer",
            next_step="no directory tie action needed",
            why="the bounded comparison did not uncover a meaningful divergence",
            risk="no_op",
        )
    )
    return matrix


def _prune_directory_tie_decision_matrix(
    matrix: list[dict[str, str]],
    *,
    max_rows: int = DEFAULT_DIRECTORY_TIE_MATRIX_ROWS,
) -> tuple[list[dict[str, str]], int]:
    if max_rows <= 0:
        max_rows = 1
    if len(matrix) <= max_rows:
        return list(matrix), 0
    keep_rows = max(1, max_rows - 1)
    pruned_rows = len(matrix) - keep_rows
    pruned_matrix = list(matrix[:keep_rows])
    pruned_matrix.append(
        _directory_tie_matrix_row(
            stage="pruned",
            condition=f"{pruned_rows} remaining branches were pruned",
            action="defer",
            next_step="rerun directory-tie-build with a narrower scope or continue interactively",
            why="the matrix was trimmed to keep memory and table sizes reasonable",
            risk="hard_stop",
        )
    )
    return pruned_matrix, pruned_rows


def _directory_tie_decision_tree(diff: DirectoryTieDiff) -> list[dict[str, str]]:
    return _directory_tie_decision_matrix(diff)


def _directory_tie_recommended_choice(diff: DirectoryTieDiff) -> str:
    branch_type = diff.classification.branch_type
    if branch_type == "gfid_conflict" and diff.next_depth_matches:
        return "collapse_shared_subtree"
    if branch_type == "child_gap" and not diff.budget.over_budget:
        return "reconcile_directory_children"
    if branch_type == "metadata_only" and not diff.budget.over_budget:
        return "repair_directory_metadata"
    if branch_type == "gfid_conflict":
        if diff.classification.child_gap_hosts and not diff.budget.over_budget:
            return "reconcile_directory_children"
        if diff.classification.metadata_mismatch_hosts and not diff.classification.missing_hosts and not diff.budget.over_budget:
            return "repair_directory_metadata"
        return "quarantine_both"
    if branch_type == "below_quorum":
        return "defer"
    if branch_type == "missing":
        return "defer"
    return diff.planner_hint or "defer"


def bounded_tree_diff_from_action(
    action: dict[str, object],
    *,
    max_depth: int = 3,
    max_children_per_dir: int = 1024,
    max_total_nodes: int = 4096,
) -> DirectoryTieDiff:
    max_depth, max_children_per_dir, max_total_nodes = normalize_directory_tie_budget(
        max_depth,
        max_children_per_dir,
        max_total_nodes,
    )
    classification = _directory_action_classification(action)
    child_names_by_host = _directory_action_child_names_by_host(action)
    budget = _directory_action_budget(
        action,
        max_depth=max_depth,
        max_children_per_dir=max_children_per_dir,
        max_total_nodes=max_total_nodes,
    )
    next_depth_matches, next_depth_signature = _shared_child_signature(child_names_by_host)
    planner_hint = str(action.get("repair_strategy") or "")
    diff_lines = [
        f"branch_type={classification.branch_type}",
        f"present_hosts={', '.join(classification.present_hosts) or 'none'}",
        f"missing_hosts={', '.join(classification.missing_hosts) or 'none'}",
        f"child_gap_hosts={', '.join(classification.child_gap_hosts) or 'none'}",
        f"unique_child_names={len(classification.unique_child_names)}",
        f"next_depth_matches={next_depth_matches}",
        f"next_depth_signature={', '.join(next_depth_signature) or 'none'}",
        f"choices={', '.join(_directory_tie_choices(classification.branch_type))}",
        f"budget=depth:{budget.max_depth}, children:{budget.max_children_per_dir}, total:{budget.max_total_nodes}",
    ]
    if budget.over_budget:
        diff_lines.append(f"budget_stop={budget.stop_reason}")
    if classification.reason:
        diff_lines.append(f"reason={classification.reason}")
    return DirectoryTieDiff(
        classification=classification,
        budget=budget,
        planner_hint=planner_hint or "review_directory_metadata",
        next_depth_matches=next_depth_matches,
        next_depth_signature=next_depth_signature,
        diff_lines=diff_lines,
    )


def build_directory_tie_report(
    plan: dict[str, object],
    *,
    max_depth: int = 3,
    max_children_per_dir: int = 1024,
    max_total_nodes: int = 4096,
    progress_stream: object | None = None,
    progress_interval_seconds: float = 60.0,
) -> dict[str, object]:
    max_depth, max_children_per_dir, max_total_nodes = normalize_directory_tie_budget(
        max_depth,
        max_children_per_dir,
        max_total_nodes,
    )
    items: list[dict[str, object]] = []
    branch_counts = Counter()
    input_source = str(plan.get("input_source") or "").strip()
    proof_scope = str(plan.get("proof_scope") or "").strip()
    canary = plan.get("canary")
    canary_identity = dict(canary) if isinstance(canary, dict) else {}
    actions = [
        action
        for action in plan.get("actions", [])
        if str(action.get("action_type") or "") in {"review_directory_children", "review_directory_gfid_conflict", "review_directory_metadata"}
    ]
    total_actions = len(actions)
    last_progress = time.monotonic()
    if progress_stream is not None:
        print(
            f"DIRECTORY TIE PROGRESS: 0/{total_actions} depth=0 status=starting",
            file=progress_stream,
            flush=True,
        )
    for index, action in enumerate(actions, start=1):
        action_type = str(action.get("action_type") or "")
        diff = bounded_tree_diff_from_action(
            action,
            max_depth=max_depth,
            max_children_per_dir=max_children_per_dir,
            max_total_nodes=max_total_nodes,
        )
        branch_counts[diff.classification.branch_type] += 1
        decision_matrix, pruned_rows = _prune_directory_tie_decision_matrix(
            _directory_tie_decision_matrix(diff)
        )
        items.append(
            {
                "logical_path": str(action.get("logical_path") or ""),
                "action_type": action_type,
                "repair_strategy": str(action.get("repair_strategy") or ""),
                "branch_type": diff.classification.branch_type,
                "planner_hint": diff.planner_hint,
                "recommended_choice": _directory_tie_recommended_choice(diff),
                "choices": _directory_tie_choices(diff.classification.branch_type),
                "decision_matrix": decision_matrix,
                "decision_matrix_pruned": pruned_rows > 0,
                "decision_matrix_pruned_rows": pruned_rows,
                "decision_tree": decision_matrix,
                "budget": asdict(diff.budget),
                "classification": asdict(diff.classification),
                "diff_lines": list(diff.diff_lines),
                "brick_roles_by_host": dict(action.get("brick_roles_by_host") or {}),
                "input_source": input_source,
                "proof_scope": proof_scope,
                "canary": canary_identity,
                "action_id": str(action.get("action_id") or ""),
                "directory_copies": [
                    dict(copy)
                    for copy in action.get("directory_copies") or []
                    if isinstance(copy, dict)
                ],
                "directory_canonical_host": str(
                    action.get("directory_canonical_host") or ""
                ),
                "directory_canonical_backend": str(
                    action.get("directory_canonical_backend") or ""
                ),
                "directory_canonical_gfid": str(
                    action.get("directory_canonical_gfid") or ""
                ),
                "directory_child_names_by_host": dict(
                    action.get("directory_child_names_by_host") or {}
                ),
                "missing_directory_children_by_host": dict(
                    action.get("missing_directory_children_by_host") or {}
                ),
                "depends_on": [
                    str(value) for value in action.get("depends_on") or [] if str(value)
                ],
                "graph_markers": [
                    str(value)
                    for value in action.get("graph_markers") or []
                    if str(value)
                ],
            }
        )
        if progress_stream is not None and (
            progress_interval_seconds <= 0 or time.monotonic() - last_progress >= progress_interval_seconds or index == total_actions
        ):
            print(
                "DIRECTORY TIE PROGRESS: "
                f"{index}/{total_actions} depth={int(action.get('depth') or 0)} "
                f"path={action.get('logical_path', '')} branch={diff.classification.branch_type} "
                f"status={diff.planner_hint}",
                file=progress_stream,
                flush=True,
            )
            last_progress = time.monotonic()
    return {
        "schema_version": 1,
        "input_source": input_source,
        "proof_scope": proof_scope,
        "canary": canary_identity,
        "directory_tie_actions": len(items),
        "branch_counts": dict(branch_counts),
        "items": items,
    }


def normalize_directory_tie_budget(
    max_depth: int,
    max_children_per_dir: int,
    max_total_nodes: int,
) -> tuple[int, int, int]:
    depth = max(1, int(max_depth))
    children = max(1, int(max_children_per_dir))
    total = max(1, int(max_total_nodes))
    return (
        min(depth, HARD_DIRECTORY_TIE_BUDGET[0]),
        min(children, HARD_DIRECTORY_TIE_BUDGET[1]),
        min(total, HARD_DIRECTORY_TIE_BUDGET[2]),
    )


def expand_directory_tie_budget(
    max_depth: int,
    max_children_per_dir: int,
    max_total_nodes: int,
) -> tuple[int, int, int]:
    depth, children, total = normalize_directory_tie_budget(
        max_depth,
        max_children_per_dir,
        max_total_nodes,
    )
    return (
        min(depth + 1, HARD_DIRECTORY_TIE_BUDGET[0]),
        min(max(children * 2, children + 1), HARD_DIRECTORY_TIE_BUDGET[1]),
        min(max(total * 2, total + 1), HARD_DIRECTORY_TIE_BUDGET[2]),
    )


def directory_tie_report_hits_budget(report: dict[str, object]) -> bool:
    for item in report.get("items", []):
        if not isinstance(item, dict):
            continue
        budget = item.get("budget")
        if isinstance(budget, dict) and budget.get("over_budget"):
            return True
        if item.get("decision_matrix_pruned"):
            return True
    return False


def prompt_directory_tie_budget_override(
    report: dict[str, object],
    *,
    current_budget: tuple[int, int, int],
    input_fn: Callable[[str], str] = input,
    output_stream: object | None = None,
) -> tuple[int, int, int] | None:
    if not directory_tie_report_hits_budget(report):
        return current_budget
    lines = [
        "directory tie comparison hit the configured budget",
        f"current budget: depth={current_budget[0]}, children={current_budget[1]}, total={current_budget[2]}",
        "effect: expanding reruns only the bounded read-only comparison; it does not change Gluster data",
        "choose [e] expand or [s] skip and keep the truncated report",
    ]
    for line in lines:
        if output_stream is None:
            print(line)
        else:
            print(line, file=output_stream)
    answer = _normalize_directory_tie_choice(input_fn("  choice [s]: "))
    if answer not in {"e", "expand", "y", "yes"}:
        return None
    next_budget = expand_directory_tie_budget(*current_budget)
    if next_budget == current_budget:
        if output_stream is None:
            print("  budget is already at the hard limit; keeping the truncated report")
        else:
            print("  budget is already at the hard limit; keeping the truncated report", file=output_stream)
        return None
    if output_stream is None:
        print(
            "  expanding budget to "
            f"depth={next_budget[0]}, children={next_budget[1]}, total={next_budget[2]}"
        )
    else:
        print(
            "  expanding budget to "
            f"depth={next_budget[0]}, children={next_budget[1]}, total={next_budget[2]}",
            file=output_stream,
        )
    return next_budget


def render_directory_tie_report(report: dict[str, object]) -> str:
    lines = [
        "DIRECTORY TIE REPORT",
        "--------------------",
        f"Directory tie actions: {report.get('directory_tie_actions', 0)}",
    ]
    input_source = str(report.get("input_source") or "").strip()
    if input_source:
        lines.append(f"input_source: {input_source}")
    proof_scope = str(report.get("proof_scope") or "").strip()
    if proof_scope:
        lines.append(f"proof_scope: {proof_scope}")
    canary = report.get("canary")
    if isinstance(canary, dict):
        identity_parts = [
            f"{field}={str(canary.get(field) or '').strip()}"
            for field in ("kind", "volume", "scenario")
            if str(canary.get(field) or "").strip()
        ]
        if identity_parts:
            lines.append("canary: " + ", ".join(identity_parts))
    branch_counts = report.get("branch_counts") or {}
    if branch_counts:
        lines.append(
            "branches: "
            + ", ".join(f"{key}={value}" for key, value in sorted(branch_counts.items()))
        )
    for item in report.get("items", []):
        lines.append("")
        lines.append(f"[{item.get('branch_type', 'unknown')}] {item.get('logical_path', '')}")
        lines.append(f"action_type: {item.get('action_type', '')}")
        lines.append(f"planner_hint: {item.get('planner_hint', '')}")
        lines.append(f"recommended_choice: {item.get('recommended_choice', '')}")
        item_input_source = str(item.get("input_source") or "").strip()
        if item_input_source:
            lines.append(f"input_source: {item_input_source}")
        lines.extend(_format_brick_roles_by_host_for_notes(item.get("brick_roles_by_host") or {}))
        choices = item.get("choices") or []
        if choices:
            lines.append("choices: " + ", ".join(str(choice) for choice in choices))
        decision_matrix = item.get("decision_matrix") or item.get("decision_tree") or []
        if decision_matrix:
            lines.append("decision matrix:")
            for idx, node in enumerate(decision_matrix, start=1):
                if not isinstance(node, dict):
                    continue
                parts = [
                    f"stage {node.get('stage', '')}",
                    f"if {node.get('if', '')}",
                    f"then {node.get('then', '')}",
                    f"next {node.get('next', '')}",
                ]
                risk = str(node.get("risk") or "").strip()
                if risk:
                    parts.append(f"risk {risk}")
                why = str(node.get("why") or "").strip()
                if why:
                    parts.append(f"why {why}")
                lines.append(f"  {idx}. " + "; ".join(parts))
        if item.get("decision_matrix_pruned"):
            pruned_rows = item.get("decision_matrix_pruned_rows", 0)
            lines.append(
                f"decision_matrix_pruned: true; rows_trimmed={pruned_rows}; "
                "matrix trimmed to keep memory and table sizes reasonable"
            )
        budget = item.get("budget") or {}
        if budget:
            lines.append(
                "budget: "
                + ", ".join(
                    f"{key}={value}"
                    for key, value in budget.items()
                    if key in {"max_depth", "max_children_per_dir", "max_total_nodes", "over_budget", "stop_reason"}
                    and value
                )
            )
        for diff_line in item.get("diff_lines") or []:
            lines.append(f"- {diff_line}")
    return "\n".join(lines).rstrip() + "\n"


def _normalize_directory_tie_choice(choice: str) -> str:
    normalized = str(choice or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"rename", "quarantine"}:
        return "quarantine_both"
    return normalized


def _directory_copy_identity(copy: dict[str, object]) -> str:
    return str(
        copy.get("identity")
        or copy.get("backend_trusted_gfid")
        or copy.get("gfid")
        or ""
    ).strip()


def _directory_copy_matches_canonical(
    copy: dict[str, object],
    *,
    canonical_gfid: str,
    canonical_host: str,
    canonical_backend: str,
) -> bool:
    identity = _directory_copy_identity(copy)
    if canonical_gfid and identity:
        return identity == canonical_gfid
    if canonical_host:
        return str(copy.get("host") or "") == canonical_host
    if canonical_backend:
        return str(copy.get("backend") or "") == canonical_backend
    return False


def _interactive_directory_tie_choices(item: dict[str, object]) -> list[str]:
    available = {
        _normalize_directory_tie_choice(choice)
        for choice in item.get("choices") or []
        if _normalize_directory_tie_choice(choice)
    }
    copies = [
        copy
        for copy in item.get("directory_copies") or []
        if isinstance(copy, dict)
        and str(copy.get("host") or "")
        and str(copy.get("backend") or "")
    ]
    choices: list[str] = []
    if copies and "quarantine_both" in available:
        choices.append("quarantine_both")
    canonical = bool(
        item.get("directory_canonical_gfid")
        or item.get("directory_canonical_host")
        or item.get("directory_canonical_backend")
    )
    if copies and canonical and "quarantine_loser" in available:
        choices.append("quarantine_loser")
    signatures = {
        tuple(sorted(str(name) for name in names if str(name)))
        for names in (item.get("directory_child_names_by_host") or {}).values()
        if isinstance(names, list) and names
    }
    if (
        "merge" in available
        and canonical
        and len(signatures) == 1
        and not (item.get("classification") or {}).get("missing_hosts")
    ):
        choices.append("merge")
    if (
        "reconcile_directory_children" in available
        and item.get("depends_on")
        and "directory_child_gap:executable" in (item.get("graph_markers") or [])
    ):
        choices.append("reconcile_directory_children")
    choices.append("defer")
    return choices


def _directory_tie_selection_preview(
    item: dict[str, object],
    selected: str,
) -> list[str]:
    # Import lazily because apply planning imports this report module.
    from .apply_planning_utils import _directory_quarantine_target

    copies = [
        copy
        for copy in item.get("directory_copies") or []
        if isinstance(copy, dict)
        and str(copy.get("host") or "")
        and str(copy.get("backend") or "")
    ]
    action_id = str(item.get("action_id") or "")
    canonical_gfid = str(item.get("directory_canonical_gfid") or "")
    canonical_host = str(item.get("directory_canonical_host") or "")
    canonical_backend = str(item.get("directory_canonical_backend") or "")
    lines = [
        "  decision effect: record this choice for a later planner/apply rebuild; "
        "this prompt does not change Gluster data"
    ]
    if selected in {"quarantine_both", "quarantine_loser"}:
        selected_copies = list(copies)
        if selected == "quarantine_loser":
            selected_copies = [
                copy
                for copy in copies
                if not _directory_copy_matches_canonical(
                    copy,
                    canonical_gfid=canonical_gfid,
                    canonical_host=canonical_host,
                    canonical_backend=canonical_backend,
                )
            ]
        lines.append("  proposed quarantine targets:")
        for copy in selected_copies:
            host = str(copy.get("host") or "")
            source = str(copy.get("backend") or "")
            identity = _directory_copy_identity(copy)
            target = _directory_quarantine_target(
                source,
                action_id=action_id,
                host=host,
                identity=identity,
                mode=selected,
            )
            lines.append(
                f"    - {host}: {source} -> {target} "
                f"(GFID {identity or 'not recorded'})"
            )
            gfid_path = str(copy.get("gfid_path") or "")
            if gfid_path and gfid_path != source:
                gfid_target = _directory_quarantine_target(
                    gfid_path,
                    action_id=action_id,
                    host=host,
                    identity=identity or source,
                    mode=selected,
                )
                lines.append(
                    f"      GFID link: {gfid_path} -> {gfid_target}"
                )
        if not selected_copies:
            lines.append(
                "    - none recorded; the planner will keep this decision review-only"
            )
        lines.append(
            "  rollback: move every generated quarantine target back to its "
            "recorded source path"
        )
    elif selected == "merge":
        lines.extend(
            [
                (
                    "  effect: attach canonical trusted.gfid "
                    f"{canonical_gfid or 'not recorded'} to conflicting "
                    "directory backends whose bounded child signatures agree"
                ),
                (
                    "  canonical source: "
                    f"{canonical_host or 'not recorded'}: "
                    f"{canonical_backend or 'backend not recorded'}"
                ),
                "  affected targets:",
            ]
        )
        mismatch_copies = [
            copy
            for copy in copies
            if canonical_gfid
            and _directory_copy_identity(copy) != canonical_gfid
        ]
        for copy in mismatch_copies:
            lines.append(
                f"    - {copy.get('host')}: {copy.get('backend')} "
                f"(current GFID {_directory_copy_identity(copy) or 'not recorded'}; "
                f"proposed GFID {canonical_gfid})"
            )
        if not mismatch_copies:
            lines.append("    - none recorded; merge will remain review-only")
        lines.append(
            "  rollback: inspect the generated xattr backup/apply artifacts; "
            "merge has no quarantine move-back step"
        )
    elif selected == "reconcile_directory_children":
        lines.append(
            "  effect: run only the attached child repair dependencies and "
            "then rescan the parent"
        )
        lines.append("  affected child gaps:")
        missing_by_host = item.get("missing_directory_children_by_host") or {}
        for host, names in sorted(missing_by_host.items()):
            if names:
                lines.append(
                    f"    - {host}: {', '.join(str(name) for name in names)}"
                )
        lines.append(
            "  dependency actions: "
            + ", ".join(str(value) for value in item.get("depends_on") or [])
        )
        lines.append(
            "  rollback: inspect each child action's backup/revert steps before execution"
        )
    lines.append(
        "  protection: apply-build must produce concrete guarded steps and "
        "apply-run still requires its own explicit execution authority"
    )
    return lines


def prompt_directory_tie_selections(
    report: dict[str, object],
    *,
    input_fn: Callable[[str], str] = input,
    output_stream: object | None = None,
) -> dict[str, str]:
    selections: dict[str, str] = {}
    for item in report.get("items", []):
        if not isinstance(item, dict):
            continue
        logical_path = str(item.get("logical_path") or "")
        if not logical_path:
            continue
        branch_type = str(item.get("branch_type") or "")
        reported_recommended = _normalize_directory_tie_choice(
            item.get("recommended_choice") or "defer"
        )
        choices = _interactive_directory_tie_choices(item)
        recommended = (
            reported_recommended
            if reported_recommended in choices
            else "quarantine_both"
            if "quarantine_both" in choices
            else "defer"
        )
        classification = item.get("classification") or {}
        prompt_lines = [
            f"{logical_path} [{branch_type}]",
            f"  diagnosis: {classification.get('reason') or 'not recorded'}",
            f"  evidence source: {item.get('input_source') or 'not recorded'}",
            f"  recommendation: {recommended}",
            "  recorded directory copies:",
        ]
        copies = [
            copy
            for copy in item.get("directory_copies") or []
            if isinstance(copy, dict)
        ]
        for copy in copies:
            role = str(
                (item.get("brick_roles_by_host") or {}).get(
                    str(copy.get("host") or "")
                )
                or ""
            )
            prompt_lines.append(
                f"    - {copy.get('host')}: {copy.get('backend')} "
                f"(GFID {_directory_copy_identity(copy) or 'not recorded'}"
                + (f"; role={role})" if role else ")")
            )
        if not copies:
            prompt_lines.append("    - none recorded")
        prompt_lines.append("  supported choices:")
        for index, choice in enumerate(choices, 1):
            prompt_lines.append(f"    [{index}] {choice}")
        prompt_lines.extend(
            [
                "    [s] skip this item (no decision recorded)",
                "  Enter alone skips; no recommendation is selected implicitly.",
            ]
        )
        for line in prompt_lines:
            if output_stream is None:
                print(line)
            else:
                print(line, file=output_stream)

        answer = _normalize_directory_tie_choice(input_fn("  choice [s]: "))
        if answer in {"", "s", "skip"}:
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            selected = choices[int(answer) - 1]
        else:
            selected = answer
        if selected not in choices:
            message = (
                f"  unsupported choice {selected}; skipped without recording a decision"
            )
            if output_stream is None:
                print(message)
            else:
                print(message, file=output_stream)
            continue
        if selected == "defer":
            selections[logical_path] = selected
            continue

        preview = _directory_tie_selection_preview(item, selected)
        for line in preview:
            if output_stream is None:
                print(line)
            else:
                print(line, file=output_stream)
        confirmation = input_fn(
            "  Type CONFIRM exactly to record this decision, or anything else to skip: "
        )
        if confirmation.strip() != "CONFIRM":
            if output_stream is None:
                print("  choice cancelled; no decision recorded")
            else:
                print("  choice cancelled; no decision recorded", file=output_stream)
            continue
        selections[logical_path] = selected
    return selections


def build_directory_tie_decisions(
    report: dict[str, object],
    *,
    selections: dict[str, str] | None = None,
) -> dict[str, dict[str, object]]:
    decisions: dict[str, dict[str, object]] = {}
    normalized_selections = {
        str(logical_path): _normalize_directory_tie_choice(choice)
        for logical_path, choice in (selections or {}).items()
        if str(logical_path).strip()
    }
    interactive_selections = selections is not None
    for item in report.get("items", []):
        if not isinstance(item, dict):
            continue
        logical_path = str(item.get("logical_path") or "").strip()
        if not logical_path:
            continue
        if interactive_selections and logical_path not in normalized_selections:
            continue
        recommended = _normalize_directory_tie_choice(item.get("recommended_choice") or "defer")
        selected = normalized_selections.get(logical_path, recommended)
        if not selected:
            selected = recommended or "defer"
        item_choices = {
            _normalize_directory_tie_choice(choice)
            for choice in item.get("choices") or []
            if _normalize_directory_tie_choice(choice)
        }
        if selected not in item_choices and selected != recommended:
            selected = recommended or "defer"
        decisions[logical_path] = {
            "directory_choice": selected,
            "recommended_choice": recommended,
            "branch_type": str(item.get("branch_type") or ""),
            "choices": sorted(item_choices),
            "decision_matrix_pruned": bool(item.get("decision_matrix_pruned")),
            "decision_matrix_pruned_rows": int(item.get("decision_matrix_pruned_rows") or 0),
            "selection_source": "operator" if normalized_selections.get(logical_path) else "recommended",
            "why": str(item.get("classification", {}).get("reason") or "") if isinstance(item.get("classification"), dict) else "",
        }
    return decisions


def write_directory_tie_report(path: str | Path, report: dict[str, object]) -> None:
    write_json_shared(path, report)
