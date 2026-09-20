# SPDX-License-Identifier: GPL-2.0-only
"""Decision-file helpers."""
from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from .install_paths import DEFAULT_SERVICE_USER
from .protocol import ChecksumBatchRequest, ChecksumTarget
from .ssh_identity import ssh_identity_options
from .volume import discover_brick_hosts
from .shared_io import write_json_shared
from .controller_cycle_report import controller_cycle_next_action, render_controller_cycle_report, summarize_controller_cycle


def _ambiguous_actions(plan: dict[str, object]) -> list[dict[str, object]]:
    return [
        action
        for action in plan.get("actions", [])
        if action.get("repair_strategy") in {
            "ambiguous_entry_split_brain_file",
            "arbiter_backed_data_identity_conflict",
        }
    ]


def _directory_mdata_review_actions(plan: dict[str, object]) -> list[dict[str, object]]:
    return [
        action
        for action in plan.get("actions", [])
        if action.get("repair_strategy") == "review_directory_mdata_state"
        and action.get("action_type") == "review_directory_metadata"
    ]


def _posix_metadata_review_actions(plan: dict[str, object]) -> list[dict[str, object]]:
    return [
        action
        for action in plan.get("actions", [])
        if action.get("repair_strategy") == "choose_posix_metadata_source"
        and action.get("action_type") == "review_posix_metadata_no_majority"
    ]


def _representative_copy_for_identity(action: dict[str, object], identity: str) -> dict[str, object] | None:
    matches = [
        copy
        for copy in action.get("file_copies", [])
        if str(copy.get("identity") or "") == identity
    ]
    if not matches:
        return None
    matches.sort(
        key=lambda item: (
            -(int(item.get("mtime") or -1)),
            -(int(item.get("size") or -1)),
            str(item.get("host") or ""),
        ),
    )
    return matches[0]


def _checksum_requests_by_host(actions: list[dict[str, object]]) -> dict[str, list[ChecksumTarget]]:
    by_host: dict[str, list[ChecksumTarget]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for action in actions:
        copies = action.get("file_copies", []) if action.get("repair_strategy") == "choose_posix_metadata_source" else []
        if not copies:
            copies = [
                _representative_copy_for_identity(action, str(cohort.get("identity") or ""))
                for cohort in action.get("file_cohorts", [])
            ]
        for copy in copies:
            if not isinstance(copy, dict):
                continue
            host = str(copy.get("host") or "")
            backend_path = str(copy.get("backend") or "")
            key = (host, backend_path)
            if not host or not backend_path or key in seen:
                continue
            seen.add(key)
            by_host[host].append(
                ChecksumTarget(
                    logical_path=str(action.get("logical_path") or ""),
                    backend_path=backend_path,
                )
            )
    return by_host


def _remote_command(parts: list[str], *, use_sudo: bool) -> str:
    command = ["sudo", "-n", *parts] if use_sudo else list(parts)
    return subprocess.list2cmdline(command)


def _run_checksum_batch(
    host: str,
    ssh_user: str,
    worker_path: str,
    items: list[ChecksumTarget],
    connect_timeout: float = 10.0,
) -> list[dict[str, str]]:
    request = ChecksumBatchRequest(items=items)
    remote_cmd = _remote_command([worker_path, "checksum-batch"], use_sudo=ssh_user != "root")
    proc = subprocess.run(
        [
            "ssh",
            *ssh_identity_options(),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={max(1, int(connect_timeout))}",
            f"{ssh_user}@{host}",
            remote_cmd,
        ],
        input=json.dumps(request.to_dict()),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"checksum worker failed on {host}: {proc.stderr.strip() or f'exit {proc.returncode}'}"
        )
    payload = json.loads(proc.stdout)
    checksums = [dict(item) for item in payload.get("checksums", []) if isinstance(item, dict)]
    for item in checksums:
        item["host"] = host
    return checksums


def build_split_brain_file_checksum_report(
    action: dict[str, object],
    *,
    volume: str,
    worker_path: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    connect_timeout: float = 10.0,
) -> dict[str, object]:
    logical_path = str(action.get("logical_path") or "")
    volume = str(volume or "").strip()
    worker_path = str(worker_path or "").strip()
    if not volume or not worker_path or not Path(worker_path).exists():
        return {
            "schema_version": 1,
            "volume": volume,
            "logical_path": logical_path,
            "outcome": "unavailable",
            "reason": "checksum worker unavailable",
            "items": [],
            "checksums": [],
            "errors": [],
        }

    requests_by_host: dict[str, list[ChecksumTarget]] = defaultdict(list)
    ordered_copies: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for copy in action.get("file_copies", []) or []:
        host = str(copy.get("host") or "")
        backend_path = str(copy.get("backend") or "")
        if not host or not backend_path:
            continue
        key = (host, backend_path)
        if key in seen:
            continue
        seen.add(key)
        requests_by_host[host].append(
            ChecksumTarget(logical_path=logical_path, backend_path=backend_path)
        )
        ordered_copies.append(
            {
                "host": host,
                "backend_path": backend_path,
                "identity": str(copy.get("identity") or ""),
                "mtime": copy.get("mtime"),
                "size": copy.get("size"),
            }
        )

    if not requests_by_host:
        return {
            "schema_version": 1,
            "volume": volume,
            "logical_path": logical_path,
            "outcome": "unavailable",
            "reason": "no checksum targets found",
            "items": [],
            "checksums": [],
            "errors": [],
        }

    checksums_by_host_path: dict[tuple[str, str], dict[str, str]] = {}
    errors: list[str] = []
    for host, items in sorted(requests_by_host.items()):
        try:
            for item in _run_checksum_batch(
                host,
                ssh_user,
                worker_path,
                items,
                connect_timeout=connect_timeout,
            ):
                checksums_by_host_path[
                    (str(item.get("host") or host), str(item.get("backend_path") or ""))
                ] = item
        except Exception as exc:  # pragma: no cover - live remote failure path
            errors.append(f"{host}: {exc}")

    items: list[dict[str, object]] = []
    checksum_values: list[str] = []
    for copy in ordered_copies:
        host = str(copy["host"])
        backend_path = str(copy["backend_path"])
        checksum_item = checksums_by_host_path.get((host, backend_path), {})
        checksum = str(checksum_item.get("sha256") or "")
        error = str(checksum_item.get("error") or "")
        if not checksum or error:
            if not error:
                error = "checksum unavailable"
            errors.append(f"{host}:{backend_path}: {error}")
        else:
            checksum_values.append(checksum)
        items.append(
            {
                "host": host,
                "backend_path": backend_path,
                "identity": copy.get("identity") or "",
                "mtime": copy.get("mtime"),
                "size": copy.get("size"),
                "sha256": checksum,
                "error": error,
            }
        )

    outcome = "unavailable"
    if checksum_values and not errors:
        if len(set(checksum_values)) == 1:
            outcome = "content-equal"
        else:
            outcome = "content-different"
    elif checksum_values and len(set(checksum_values)) == 1 and len(checksum_values) == len(items):
        outcome = "content-equal"

    return {
        "schema_version": 1,
        "volume": volume,
        "logical_path": logical_path,
        "outcome": outcome,
        "reason": (
            "tied copies have identical checksums"
            if outcome == "content-equal"
            else "tied copies have different checksums"
            if outcome == "content-different"
            else "checksum evidence unavailable"
        ),
        "items": items,
        "checksums": checksum_values,
        "errors": errors,
    }


def build_decision_report(
    plan: dict[str, object],
    *,
    volume: str,
    worker_path: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    hosts: list[str] | None = None,
    status: dict[str, object] | None = None,
) -> dict[str, object]:
    actions = _ambiguous_actions(plan) + _directory_mdata_review_actions(plan) + _posix_metadata_review_actions(plan)
    controller_summary = summarize_controller_cycle(status or {})
    controller_next_action = controller_cycle_next_action(controller_summary)
    if hosts is None:
        hosts = discover_brick_hosts(volume)
    requests_by_host = _checksum_requests_by_host(actions)
    checksums_by_host_path: dict[tuple[str, str], dict[str, str]] = {}
    for host, items in sorted(requests_by_host.items()):
        if host not in hosts:
            continue
        for item in _run_checksum_batch(host, ssh_user, worker_path, items):
            checksums_by_host_path[(str(item.get("host") or host), str(item.get("backend_path") or ""))] = item

    report_items: list[dict[str, object]] = []
    decisions: dict[str, dict[str, object]] = {}
    outcome_counts = Counter()

    for action in actions:
        if action.get("repair_strategy") == "choose_posix_metadata_source":
            logical_path = str(action.get("logical_path") or "")
            metadata_by_host = {
                str(host): dict(value)
                for host, value in (action.get("metadata_tuple_by_host") or {}).items()
                if str(host) and isinstance(value, dict)
            }
            backend_by_host = {
                str(host): str(value or "")
                for host, value in (action.get("metadata_backend_by_host") or {}).items()
                if str(host)
            }
            gluster_visible_metadata_split_brain = bool(action.get("gluster_visible_metadata_split_brain"))
            resolution_mode = (
                "native_source_brick"
                if gluster_visible_metadata_split_brain or str(action.get("metadata_source_reason") or "") == "native_gluster_source"
                else "direct_alignment"
            )
            checksum_evidence = []
            checksum_values = []
            checksum_errors = []
            for copy in action.get("file_copies", []) or []:
                host = str(copy.get("host") or "")
                backend_path = str(copy.get("backend") or "")
                checksum_item = checksums_by_host_path.get((host, backend_path), {})
                checksum = str(checksum_item.get("sha256") or "")
                error = str(checksum_item.get("error") or "")
                checksum_evidence.append({"host": host, "backend_path": backend_path, "sha256": checksum, "error": error})
                if checksum:
                    checksum_values.append(checksum)
                if error or not checksum:
                    checksum_errors.append(f"{host}:{error or 'checksum unavailable'}")
            checksum_outcome = "unavailable" if checksum_errors else "content-equal" if checksum_values and len(set(checksum_values)) == 1 else "content-different"
            report_items.append({
                "logical_path": logical_path,
                "outcome": "no-majority" if metadata_by_host else "unavailable",
                "decision": {},
                "recommendation": "review_or_source_choice" if controller_next_action == "continue" else controller_next_action,
                "controller_next_action": controller_next_action,
                "gluster_visible_metadata_split_brain": gluster_visible_metadata_split_brain,
                "resolution_mode": resolution_mode,
                "checksum_outcome": checksum_outcome,
                "checksum_evidence": checksum_evidence,
                "posix_metadata_by_host": [
                    {"host": host, "backend_path": backend_by_host.get(host, ""), **metadata_by_host[host]}
                    for host in sorted(metadata_by_host)
                ],
                "operator_choices": ["metadata_source_host", "keep review"],
                "reason": (
                    "POSIX metadata has no strict majority; choose a source brick explicitly before native source-brick resolution"
                    if resolution_mode == "native_source_brick"
                    else "POSIX metadata has no strict majority; choose a source host explicitly before repair"
                ),
            })
            outcome_counts["no-majority"] += 1
            continue
        if action.get("repair_strategy") == "review_directory_mdata_state":
            logical_path = str(action.get("logical_path") or "")
            directory_mdata_by_host = {
                str(host): str(value or "")
                for host, value in (action.get("directory_mdata_by_host") or {}).items()
                if str(host)
            }
            backend_by_host = {
                str(host): str(value or "")
                for host, value in (action.get("directory_backend_by_host") or {}).items()
                if str(host)
            }
            report_items.append(
                {
                    "logical_path": logical_path,
                    "current_winner_gfid": str(action.get("directory_canonical_gfid") or ""),
                    "current_winner_host": str(action.get("directory_canonical_host") or ""),
                    "outcome": "no-majority" if directory_mdata_by_host else "unavailable",
                    "decision": {},
                    "recommendation": "review_or_source_choice"
                    if controller_next_action == "continue"
                    else controller_next_action,
                    "controller_next_action": controller_next_action,
                    "mdata_by_host": [
                        {
                            "host": host,
                            "backend_path": backend_by_host.get(host, ""),
                            "mdata_hex": directory_mdata_by_host.get(host, ""),
                        }
                        for host in sorted(directory_mdata_by_host)
                    ],
                    "operator_choices": [
                        "mdata_source_host",
                        "mdata_source_value",
                        "heal/rescan",
                        "keep review",
                    ],
                    "reason": (
                        "trusted.glusterfs.mdata has no clear majority; choose a source brick or source value explicitly"
                    ),
                }
            )
            outcome_counts["no-majority"] += 1
            continue

        cohorts_report: list[dict[str, object]] = []
        checksum_values: list[str] = []
        valid_checksums = True
        for cohort in action.get("file_cohorts", []):
            identity = str(cohort.get("identity") or "")
            representative = _representative_copy_for_identity(action, identity)
            if not representative:
                cohorts_report.append(
                    {
                        "identity": identity,
                        "error": "no representative file copy found",
                    }
                )
                valid_checksums = False
                continue
            host = str(representative.get("host") or "")
            backend_path = str(representative.get("backend") or "")
            checksum_item = checksums_by_host_path.get((host, backend_path), {})
            checksum = str(checksum_item.get("sha256") or "")
            error = str(checksum_item.get("error") or "")
            if not checksum or error:
                valid_checksums = False
            else:
                checksum_values.append(checksum)
            cohorts_report.append(
                {
                    "identity": identity,
                    "hosts": list(cohort.get("hosts") or []),
                    "latest_mtime": cohort.get("latest_mtime"),
                    "largest_size": cohort.get("largest_size"),
                    "representative_host": host,
                    "representative_backend": backend_path,
                    "sha256": checksum,
                    "error": error,
                }
            )

        logical_path = str(action.get("logical_path") or "")
        current_winner = str(action.get("winner_file_gfid") or "")
        decision: dict[str, object] = {}
        outcome = "unresolved"
        if valid_checksums and checksum_values:
            if len(set(checksum_values)) == 1:
                outcome = "content-equal"
                decision = {
                    "keep_gfid": current_winner,
                    "reason": "tied cohorts have identical content checksums",
                }
                decisions[logical_path] = decision
            else:
                outcome = "content-different"
                decision = {}
        outcome_counts[outcome] += 1
        recommendation = "review_or_quarantine" if outcome == "content-different" else ""
        if controller_next_action != "continue" and outcome != "content-equal":
            recommendation = controller_next_action
        arbiter_backed_conflict = (
            action.get("repair_strategy") == "arbiter_backed_data_identity_conflict"
        )
        report_items.append(
            {
                "logical_path": logical_path,
                "current_winner_gfid": current_winner,
                "current_winner_host": action.get("winner_host") or "",
                "outcome": outcome,
                "decision": decision,
                "recommendation": recommendation,
                "recommended_choice": (
                    str(action.get("recommended_choice") or "")
                    if arbiter_backed_conflict
                    else ""
                ),
                "operator_choices": (
                    ["quarantine_loser", "quarantine_both", "keep_review", "skip"]
                    if arbiter_backed_conflict
                    else []
                ),
                "reason": (
                    "The matching data brick plus arbiter identity is the tie-breaker for the payload source; preserve the conflicting data copy with reversible quarantine, copy from that data brick, then replan."
                    if arbiter_backed_conflict
                    else ""
                ),
                "controller_next_action": controller_next_action,
                "cohorts": cohorts_report,
            }
        )

    return {
        "schema_version": 1,
        "volume": volume,
        "ambiguous_actions": len(actions),
        "hosts": hosts,
        "controller_cycle": {
            **controller_summary,
            "controller_next_action": controller_next_action,
        },
        "controller_cycle_next_action": controller_next_action,
        "controller_cycle_report": render_controller_cycle_report(status or {}),
        "outcomes": dict(outcome_counts),
        "items": report_items,
        "decisions": decisions,
    }


def write_decision_report(path: str | Path, report: dict[str, object]) -> None:
    write_json_shared(path, report)


def write_decision_file(path: str | Path, report: dict[str, object]) -> None:
    payload = {
        "decisions": report.get("decisions", {}),
    }
    write_json_shared(path, payload)
