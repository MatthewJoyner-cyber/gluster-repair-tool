# SPDX-License-Identifier: GPL-2.0-only
"""Review-action planning dispatcher for Gluster repair apply results."""
from __future__ import annotations

import json

from .apply import DEFAULT_REVIEW_POLICIES
from .apply import DEFAULT_SPLIT_BRAIN_NATIVE_POLICY
from .apply_planning_file import _plan_repair_file
from .apply_planning_file import _plan_repair_file_metadata
from .apply_planning_file import _plan_repair_posix_metadata
from .apply_planning_review import _plan_cleanup_dead_file_refs
from .apply_planning_review import _plan_cleanup_dead_gfid
from .apply_planning_review import _plan_cleanup_orphaned_symlink
from .apply_planning_review import _plan_cleanup_stale_glusterfs_index
from .apply_planning_review import _plan_delete_review_directory
from .apply_planning_review import _plan_delete_review_file
from .apply_planning_review import _plan_quarantine_type_mismatch
from .apply_planning_review import _plan_quarantine_file_conflict
from .apply_planning_review import _plan_quarantine_directory_conflict
from .apply_planning_review import _plan_reconcile_directory
from .apply_planning_utils import _action_with_decision_winner
from .apply_planning_utils import _directory_tie_decision_choice
from .apply_planning_utils import _directory_tie_effective_choice
from .apply_planning_utils import _directory_merge_is_safe
from .apply_planning_utils import _directory_conflict_mismatch_hosts
from .apply_planning_utils import _file_quarantine_decision_choice
from .apply_planning_utils import _file_metadata_mismatch_hosts_from_action
from .apply_planning_utils import _brick_root_from_backend
from .apply_planning_utils import _gluster_split_brain_preview
from .apply_planning_utils import _normalize_split_brain_policy
from .apply_planning_utils import _normalize_split_brain_native_policy
from .apply_planning_utils import _normalize_decision
from .apply_planning_utils import _resolve_file_decision_copy
from .apply_planning_utils import _select_split_brain_file_copy
from .apply_planning_utils import _split_brain_file_recovery_mode
from .apply_planning_utils import _step_id
from .apply_planning_utils import _ssh_set_mdata_preview
from .apply_planning_utils import _ssh_rm_preview
from .apply_planning_utils import _ssh_setfattr_preview
from .remote_ops import rsync_brick_pull_command
from .install_paths import DEFAULT_SERVICE_USER, DEFAULT_WORKER_PATH
from .models import ApplyActionResult, ApplyStep


def _stale_survivor_replica_count(action: dict[str, object]) -> int:
    hosts: set[str] = set()
    for field in ("healthy_hosts", "stale_hosts", "missing_hosts", "conflict_hosts"):
        for host in action.get(field) or []:
            host_text = str(host).strip()
            if host_text:
                hosts.add(host_text)
    return len(hosts)


def _normalize_mdata_hex(value: object) -> str:
    cleaned = str(value or "").strip().lower()
    if not cleaned:
        return ""
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    if not cleaned:
        return ""
    return f"0x{cleaned}"


def _directory_mdata_source_decision(
    action: dict[str, object],
    decision: dict[str, object],
) -> tuple[dict[str, object] | None, str]:
    mdata_by_host = {
        str(host): _normalize_mdata_hex(value)
        for host, value in (action.get("directory_mdata_by_host") or {}).items()
        if str(host)
    }
    source_host = str(
        decision.get("mdata_source_host")
        or decision.get("source_host")
        or decision.get("keep_host")
        or ""
    ).strip()
    source_value = _normalize_mdata_hex(
        decision.get("mdata_source_value")
        or decision.get("source_value")
        or decision.get("keep_mdata")
        or ""
    )
    if source_host:
        host_value = mdata_by_host.get(source_host, "")
        if not host_value:
            return (None, f"decision mdata_source_host={source_host} has no recorded trusted.glusterfs.mdata value")
        if source_value and source_value != host_value:
            return (
                None,
                f"decision mdata_source_value={source_value} does not match {source_host} value {host_value}",
            )
        source_value = host_value
    if not source_value:
        return (None, "decision file must provide mdata_source_host or mdata_source_value")

    candidate_hosts = [
        str(host)
        for host in action.get("healthy_hosts") or []
        if str(host)
    ]
    if not candidate_hosts:
        candidate_hosts = sorted(mdata_by_host)
    mismatch_hosts = [
        host
        for host in candidate_hosts
        if _normalize_mdata_hex(mdata_by_host.get(host, "")) != source_value
    ]
    if not mismatch_hosts:
        return (None, f"decision mdata source {source_host or source_value} leaves no mismatched hosts to align")
    promoted_action = dict(action)
    promoted_action["action_type"] = "repair_directory_metadata"
    promoted_action["repair_strategy"] = "attach_directory_mdata"
    promoted_action["directory_canonical_mdata_hex"] = source_value
    promoted_action["directory_mdata_mismatch_hosts"] = mismatch_hosts
    promoted_action.setdefault("notes", [])
    promoted_action["notes"] = list(promoted_action.get("notes") or [])
    promoted_action["notes"].append(
        f"operator selected directory mdata source: {source_host or source_value}"
    )
    promoted_action["notes"].append(
        "operator-source mdata repair: align only the chosen trusted.glusterfs.mdata value; do not touch trusted.gfid or children"
    )
    return (promoted_action, "")

def _posix_metadata_source_decision(
    action: dict[str, object],
    decision: dict[str, object],
) -> tuple[dict[str, object] | None, str]:
    metadata_by_host = {
        str(host): dict(record)
        for host, record in (action.get("metadata_tuple_by_host") or {}).items()
        if str(host) and isinstance(record, dict)
    }
    source_host = str(
        decision.get("metadata_source_host")
        or decision.get("source_host")
        or decision.get("keep_host")
        or ""
    ).strip()
    if not source_host:
        return (None, "decision file must provide metadata_source_host")
    source_metadata = metadata_by_host.get(source_host)
    if not source_metadata:
        return (None, f"decision metadata_source_host={source_host} has no recorded POSIX metadata tuple")
    mismatch_hosts = [
        host for host, metadata in sorted(metadata_by_host.items())
        if host != source_host and metadata != source_metadata
    ]
    if not mismatch_hosts:
        return (None, f"decision metadata source {source_host} leaves no mismatched hosts to align")
    backend_by_host = {
        str(host): str(backend)
        for host, backend in (action.get("metadata_backend_by_host") or {}).items()
        if str(host) and str(backend)
    }
    if source_host not in backend_by_host:
        return (None, f"decision metadata_source_host={source_host} has no recorded backend path")
    native_gluster_visible = bool(action.get("gluster_visible_metadata_split_brain")) or str(action.get("metadata_source_reason") or "") == "native_gluster_source"
    promoted_action = dict(action)
    promoted_action["action_type"] = "repair_posix_metadata"
    promoted_action["metadata_source_host"] = source_host
    promoted_action["metadata_source_backend"] = backend_by_host[source_host]
    promoted_action["metadata_fields_to_align"] = list(action.get("metadata_fields_to_align") or [])
    promoted_action["metadata_mismatch_hosts"] = mismatch_hosts
    promoted_action["notes"] = list(action.get("notes") or []) + [
        f"operator selected POSIX metadata source host: {source_host}",
    ]
    if native_gluster_visible:
        promoted_action["repair_strategy"] = "resolve_posix_metadata_source_brick"
        promoted_action["metadata_source_reason"] = "native_gluster_source"
        promoted_action["notes"].append(
            "native POSIX split-brain: resolve with Gluster source-brick after selecting the source brick"
        )
    else:
        promoted_action["repair_strategy"] = "align_posix_metadata_selected_source"
        promoted_action["metadata_source_reason"] = "operator_choice"
        promoted_action["notes"].append(
            "operator-source POSIX repair: align only recorded POSIX metadata fields; do not touch content or identity"
        )
    return (promoted_action, "")



def _default_review_recommendation(action: dict[str, object]) -> tuple[str, str]:
    """Give every review result a concrete next edge instead of a dead end."""
    action_type = str(action.get("action_type") or "")
    strategy = str(action.get("repair_strategy") or "")
    explicit_choice = str(action.get("recommended_choice") or "").strip()
    explicit_reason = str(action.get("recommended_reason") or "").strip()
    if explicit_choice:
        return explicit_choice, explicit_reason or "The canary or planner supplied this preserved-evidence recommendation."

    if action_type == "review_entry_split_brain":
        if strategy == "ambiguous_file_presence_tie":
            return (
                "quarantine_both",
                "Exactly half of the replicas carry the file, so neither restore nor delete has strict-majority authority; preserve every visible copy before deciding.",
            )
        if action.get("object_type") in {"directory", "directory_candidate"} or strategy == "review_entry_split_brain_directory":
            has_canonical = bool(
                action.get("directory_canonical_host")
                or action.get("directory_canonical_backend")
                or action.get("directory_canonical_gfid")
            )
            if has_canonical:
                return (
                    "quarantine_loser",
                    "A canonical directory side is recorded; quarantine the losing tree before any merge or subtree decision.",
                )
            return (
                "quarantine_both",
                "The directory identity is unresolved; quarantine both trees to preserve evidence before choosing merge, prune, or a source.",
            )
        return (
            "quarantine_both",
            "The file cohorts are unresolved; quarantine both copies to preserve both histories before selecting a GFID or source.",
        )

    if action_type == "review_probable_stale_survivor":
        if action.get("object_type") in {"directory", "directory_candidate"} or strategy == "delete_below_quorum_subtree":
            return (
                "delete_below_quorum_subtree",
                "The subtree is below quorum; review the host evidence, then remove stale children deepest-first if the deletion is confirmed.",
            )
        return (
            "delete_below_quorum_file",
            "The file is below quorum; review the surviving and missing hosts, then remove stale backend/GFID residue if the deletion is confirmed.",
        )

    if action_type == "review_probable_orphaned_symlink":
        return (
            "delete_orphaned_symlink_residue",
            "The symlink is below quorum or orphaned; preserve the evidence, then remove the symlink backend/GFID residue if confirmed.",
        )

    if action_type == "review_directory_gfid_conflict":
        if strategy == "ambiguous_directory_presence_tie":
            return (
                "quarantine_both",
                "Exactly half of the replicas carry the directory, so neither restore nor subtree deletion has strict-majority authority; preserve every visible tree before deciding.",
            )
        has_canonical = bool(
            action.get("directory_canonical_host")
            or action.get("directory_canonical_backend")
            or action.get("directory_canonical_gfid")
        )
        return (
            "quarantine_loser" if has_canonical else "quarantine_both",
            (
                "A canonical directory side is recorded; quarantine the losing tree before any merge."
                if has_canonical
                else "No directory winner is proven; quarantine both trees before any merge, prune, or source decision."
            ),
        )

    if action_type == "review_directory_children":
        if strategy == "verify_directory_child_absence":
            return (
                "recover_missing_child_or_confirm_delete",
                "No child copy survives. Recover it from an authoritative backup if it should exist; otherwise confirm deletion and clean only separately proven stale child/index residue.",
            )
        if action.get("depends_on") and "directory_child_gap:executable" in (action.get("graph_markers") or []):
            return (
                "reconcile_directory_children",
                "Run the concrete child repairs first, then rebuild the manifest and plan from fresh evidence.",
            )
        return (
            "collect_gfid_child_evidence",
            "Use immediate --gfid-child evidence with mount probing off so the planner learns the child GFID/type before proposing a repair.",
        )

    if action_type == "review_directory_metadata":
        if strategy == "review_directory_mdata_state":
            return (
                "choose_directory_mdata_source",
                "Choose an explicit trusted.glusterfs.mdata source value or let heal/rescan run before considering metadata alignment.",
            )
        return (
            "refresh_directory_evidence",
            "Run focused mount, child-name, GFID, and directory-tie evidence collection, then rebuild the plan.",
        )

    if action_type == "review_unclassified_authority":
        return (
            "refresh_and_close_authority_gap",
            "Refresh the bounded evidence, then classify the preserved candidate before any write is considered; skip/defer remains a no-change closure.",
        )

    if action_type == "review_file_metadata":
        return (
            "enable_file_metadata_repair_policy",
            "Confirm canonical GFID evidence, rerun with --policy-file-metadata repair, and let the planner promote only the metadata attach branch.",
        )

    if action_type == "review_posix_metadata_no_majority":
        return (
            "choose_posix_metadata_source",
            "Select the authoritative POSIX tuple explicitly before aligning any replica.",
        )

    if action_type == "review_type_mismatch":
        return (
            "quarantine_both",
            "The object type is unresolved; quarantine both file and directory branches before selecting a source.",
        )

    if action_type in {"review_dead_gfid_reference", "defer_dead_gfid_cleanup"}:
        return (
            "follow_live_reference",
            "Follow the GFID reference to its terminal object before deciding whether residue is stale.",
        )

    return (
        "create_support_case",
        "This review shape has no local repair proof; create a bounded support handoff with the manifest, plan, logs, and current heal evidence.",
    )


def _plan_review_action(
    action: dict[str, object],
    *,
    execution_mode: str,
    backup_root: str | None,
    backup_mode: str,
    batch: bool,
    review_policies: dict[str, str],
    split_brain_native_policy: str = DEFAULT_SPLIT_BRAIN_NATIVE_POLICY,
    decisions: dict[str, dict[str, object]] | None = None,
    volume: str = "",
    brick_path: str = "",
    worker_path: str = str(DEFAULT_WORKER_PATH),
    ssh_user: str = DEFAULT_SERVICE_USER,
) -> ApplyActionResult:
    action_id = str(action["action_id"])
    logical_path = str(action["logical_path"])
    action_type = str(action["action_type"])
    repair_strategy = str(action.get("repair_strategy") or "")
    winner_host = str(action.get("winner_host") or "")
    winner_backend = str(action.get("winner_backend") or "")
    mounted_target = str(action.get("mounted_target") or "")

    result = ApplyActionResult(
        action_id=action_id,
        logical_path=logical_path,
        action_type=action_type,
        execution_mode=execution_mode,
        backup_root=backup_root or "",
        backup_mode=backup_mode,
        batch=batch,
        status="review",
        depends_on=list(action.get("depends_on") or []),
        native_heal_first=bool(action.get("native_heal_first")),
        native_heal_fallback_action=str(action.get("native_heal_fallback_action") or ""),
        native_heal_reason=str(action.get("native_heal_reason") or ""),
        decision=_normalize_decision(action, decisions),
        notes=list(action.get("notes") or []),
    )
    result.recommended_choice, result.recommended_reason = _default_review_recommendation(action)
    result.metadata_source_host = str(action.get("metadata_source_host") or "")
    result.metadata_source_backend = str(action.get("metadata_source_backend") or "")
    result.metadata_source_reason = str(action.get("metadata_source_reason") or "")
    result.metadata_fields_to_align = [str(field) for field in action.get("metadata_fields_to_align") or [] if str(field)]
    result.brick_roles_by_host = {
        str(host): str(role)
        for host, role in (action.get("brick_roles_by_host") or {}).items()
        if str(host) and str(role)
    }
    result.brick_host_aliases = {
        str(host): [str(alias) for alias in aliases if str(alias)]
        for host, aliases in (action.get("brick_host_aliases") or {}).items()
        if str(host) and isinstance(aliases, list)
    }
    if repair_strategy:
        result.notes.append(f"repair strategy: {repair_strategy}")
    if backup_root:
        result.notes.append(f"backup root override: {backup_root}")
    result.notes.append("review-only action; no filesystem steps are planned yet")

    healthy_hosts = list(action.get("healthy_hosts") or [])
    missing_hosts = list(action.get("missing_hosts") or [])
    conflict_hosts = list(action.get("conflict_hosts") or [])
    stale_hosts = list(action.get("stale_hosts") or [])

    if action_type == "review_entry_split_brain":
        if action.get("object_type") in {"directory", "directory_candidate"} or repair_strategy == "review_entry_split_brain_directory":
            policy = review_policies.get("entry_split_brain_directory", DEFAULT_REVIEW_POLICIES["entry_split_brain_directory"])
            result.notes.append(
                "interactive choices: keep for review, quarantine the losing directory tree, or skip"
            )
            result.notes.append(f"configured batch policy: {policy}")
            if batch and policy == "review":
                result.notes.append(
                    "batch default: keep review-only for now; do not automatically quarantine or delete directory trees"
                )
            elif batch and policy == "rename":
                result.notes.append(
                    "batch default: quarantine the losing directory tree after review data is accepted"
                )
            elif batch and policy == "skip":
                result.notes.append("batch default: skip this review action")
            else:
                result.notes.append(
                    "interactive default suggestion: quarantine the losing directory tree only after explicit review"
                )
            if healthy_hosts:
                result.notes.append(f"present hosts: {', '.join(healthy_hosts)}")
            if conflict_hosts:
                result.notes.append(f"conflicting hosts: {', '.join(conflict_hosts)}")
            if missing_hosts:
                result.notes.append(f"missing hosts: {', '.join(missing_hosts)}")
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "review-entry-split-brain-directory"),
                    step_type="review_entry_split_brain_directory",
                    target_path=mounted_target or logical_path,
                    notes=[
                        f"present hosts: {', '.join(healthy_hosts) if healthy_hosts else 'none'}",
                        f"conflict hosts: {', '.join(conflict_hosts) if conflict_hosts else 'none'}",
                        "default action: review and quarantine the losing directory tree before any subtree reconcile decision",
                    ],
                )
            )
            return result
        policy = review_policies.get("entry_split_brain_file", DEFAULT_REVIEW_POLICIES["entry_split_brain_file"])
        if result.decision:
            result.notes.append(f"decision file entry loaded: {json.dumps(result.decision, sort_keys=True)}")
        normalized_policy = _normalize_split_brain_policy(policy)
        decision_choice = _file_quarantine_decision_choice(result.decision)
        if repair_strategy == "arbiter_backed_data_identity_conflict":
            if decision_choice in {"quarantine_both", "quarantine_loser"}:
                proposed = _plan_quarantine_file_conflict(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                    quarantine_mode=decision_choice,
                    quarantine_only=decision_choice == "quarantine_both",
                )
                proposed.action_type = action_type
                proposed.decision = result.decision
                proposed.notes.insert(0, "proposed repair from explicit arbiter-backed conflict decision; inspect before execution")
                proposed.notes.insert(1, f"guided quarantine choice: {decision_choice}")
                if decision_choice == "quarantine_loser":
                    proposed.notes.insert(
                        2,
                        "the matching data brick plus arbiter identity authorizes this source copy; the arbiter is never a payload source",
                    )
                    resolver_preview = _gluster_split_brain_preview(
                        volume,
                        logical_path,
                        winner_host=winner_host,
                        brick_path=brick_path or _brick_root_from_backend(winner_backend, logical_path),
                        native_policy="source-brick",
                    )
                    if resolver_preview:
                        proposed.steps.append(
                            ApplyStep(
                                step_id=_step_id(action_id, len(proposed.steps) + 1, "clear-split-brain-marker"),
                                step_type="resolve_split_brain_gluster_cli",
                                host=winner_host,
                                source_path=winner_backend,
                                target_path=mounted_target or logical_path,
                                command_preview=resolver_preview,
                                notes=[
                                    "run only after the evidence-authorized quarantine, payload copy, and canonical GFID attachment",
                                    "clear Gluster's remaining split-brain state and restore client visibility; this command does not choose the source",
                                    "the data-brick source was already authorized by the matching data-plus-arbiter GFID evidence; the arbiter is never a payload source",
                                    "if Gluster reports this resolver inconclusive or it fails, stop and rebuild fresh evidence rather than selecting another source",
                                ],
                            )
                        )
                        proposed.notes.append(
                            "after the explicit copy, use the official source-brick resolver only to clear Gluster's stale split-brain marker and restore mount visibility"
                        )
                    else:
                        proposed.notes.append(
                            "official split-brain marker clearing was not planned because volume, source data host, or brick path is incomplete; rebuild evidence rather than guessing a resolver source"
                        )
                return proposed
            result.notes.append(
                "the matching data brick plus arbiter identity authorizes an explicit source copy after reversible quarantine; ordinary index heal is not used to choose the source"
            )
            result.notes.append(
                "interactive choices: quarantine the conflicting data copy, quarantine both copies, keep for review, or skip"
            )
            result.notes.append("recommended next step: quarantine the conflicting data copy, explicitly copy the matching data payload back, then rebuild evidence")
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "review-arbiter-data-identity-conflict"),
                    step_type="review_arbiter_data_identity_conflict",
                    target_path=mounted_target or logical_path,
                    notes=[
                        "matching data plus arbiter identity authorizes the data-brick source; the arbiter is never a payload source",
                        "do not rely on ordinary index heal to choose the source or reconstruct the conflict",
                        "recommended action: quarantine_loser, explicitly copy the selected data payload, then rebuild evidence",
                    ],
                )
            )
            return result
        if decision_choice in {"quarantine_both", "quarantine_loser"}:
            proposed = _plan_quarantine_file_conflict(
                action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
                quarantine_mode=decision_choice,
            )
            proposed.action_type = action_type
            proposed.decision = result.decision
            proposed.notes.insert(0, "proposed action from explicit guided quarantine decision; inspect before execution")
            proposed.notes.insert(1, f"guided quarantine choice: {decision_choice}")
            if decision_choice == "quarantine_loser":
                proposed.notes.insert(2, "decision file selected quarantine_loser; preserve the canonical side and quarantine only the divergent copies")
            return proposed
        if normalized_policy == "quarantine":
            quarantine_mode = "quarantine_both"
            if result.decision:
                decision_choice = _file_quarantine_decision_choice(result.decision)
                if decision_choice in {"quarantine_both", "quarantine_loser"}:
                    quarantine_mode = decision_choice
            proposed = _plan_quarantine_file_conflict(
                action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
                quarantine_mode=quarantine_mode,
            )
            proposed.decision = result.decision
            proposed.notes.insert(0, f"configured batch policy: {normalized_policy}")
            proposed.notes.insert(1, "proposed action from test-only preserve-all policy; inspect before execution")
            if quarantine_mode == "quarantine_loser":
                proposed.notes.insert(2, "decision file selected quarantine_loser; preserve the canonical side and quarantine only the divergent copies")
            if result.decision:
                insert_index = 3 if quarantine_mode == "quarantine_loser" else 2
                proposed.notes.insert(insert_index, f"decision file entry loaded: {json.dumps(result.decision, sort_keys=True)}")
            return proposed
        heal_split_marker = "heal_info_marks_split_brain" in {
            str(note) for note in action.get("notes") or []
        }
        if heal_split_marker and not result.decision and not decision_choice and normalized_policy != "quarantine":
            result.notes.append(
                "heal info marked this file as split-brain; the shared GFID does not prove identical content"
            )
            result.notes.append(
                "recommended next step: quarantine both file copies, then checksum the preserved copies before choosing a source"
            )
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "review-heal-marked-content-split"),
                    step_type="review_heal_marked_content_split",
                    target_path=mounted_target or logical_path,
                    notes=[
                        "preserve both file histories before source selection",
                        "recommended action: quarantine_both",
                        "compare checksums of the preserved copies before any keep_gfid or keep_host decision",
                    ],
                )
            )
            return result
        if repair_strategy == "ambiguous_file_presence_tie":
            result.notes.append(
                "exactly half of the replicas carry the file; auto source selection is disabled because a single visible cohort does not prove restore or delete intent"
            )
            result.notes.append(
                "recommended next step: quarantine all visible copies, then make an explicit restore-or-delete decision from preserved evidence"
            )
            result.notes.append(f"configured batch policy: {normalized_policy}")
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "review-file-presence-tie"),
                    step_type="review_file_presence_tie",
                    target_path=mounted_target or logical_path,
                    notes=[
                        "client quorum is not strict-majority repair authority",
                        "recommended action: quarantine_both",
                        "do not auto-restore or auto-delete the half-present cohort",
                    ],
                )
            )
            return result
        if repair_strategy == "ambiguous_entry_split_brain_file":
            selected, error = _resolve_file_decision_copy(action, result.decision)
            if selected:
                recovery_mode, recovery_reason = _split_brain_file_recovery_mode(
                    action,
                    policy,
                    backup_mode=backup_mode,
                )
                overridden_action = _action_with_decision_winner(action, selected)
                if recovery_mode == "quarantine_loser":
                    proposed = _plan_quarantine_file_conflict(
                        overridden_action,
                        execution_mode=execution_mode,
                        backup_root=backup_root,
                        backup_mode=backup_mode,
                        batch=batch,
                        quarantine_mode="quarantine_loser",
                    )
                    proposed.action_type = action_type
                    proposed.decision = result.decision
                    proposed.notes.insert(0, "configured batch policy: replace")
                    proposed.notes.insert(1, f"split-brain recovery mode: {recovery_reason}")
                    proposed.notes.insert(2, "proposed action from explicit decision file; inspect before execution")
                    return proposed
                overridden_action["repair_strategy"] = "replace_entry_split_brain_file"
                proposed = _plan_repair_file(
                    overridden_action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                    volume=volume,
                    brick_path=brick_path,
                    split_brain_native_policy=split_brain_native_policy,
                    native_split_brain_auto_followup=False,
                    worker_path=worker_path,
                    ssh_user=ssh_user,
                )
                proposed.action_type = action_type
                proposed.status = "proposed"
                proposed.decision = result.decision
                proposed.notes.insert(0, "configured batch policy: replace")
                proposed.notes.insert(1, f"split-brain recovery mode: {recovery_reason}")
                proposed.notes.insert(2, "proposed action from explicit decision file; inspect before execution")
                return proposed
            selected, selection_reason = _select_split_brain_file_copy(action, policy)
            if selected:
                recovery_mode, recovery_reason = _split_brain_file_recovery_mode(
                    action,
                    policy,
                    backup_mode=backup_mode,
                )
                overridden_action = _action_with_decision_winner(action, selected)
                if recovery_mode == "quarantine_loser":
                    proposed = _plan_quarantine_file_conflict(
                        overridden_action,
                        execution_mode=execution_mode,
                        backup_root=backup_root,
                        backup_mode=backup_mode,
                        batch=batch,
                        quarantine_mode="quarantine_loser",
                    )
                    proposed.action_type = action_type
                    proposed.status = "proposed"
                    proposed.decision = result.decision
                    proposed.notes.insert(0, f"configured batch policy: {_normalize_split_brain_policy(policy)}")
                    proposed.notes.insert(1, f"split-brain winner selected by policy: {selection_reason}")
                    proposed.notes.insert(2, f"split-brain recovery mode: {recovery_reason}")
                    return proposed
                proposed = _plan_repair_file(
                    overridden_action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                    volume=volume,
                    brick_path=brick_path,
                    split_brain_native_policy=split_brain_native_policy,
                    worker_path=worker_path,
                    ssh_user=ssh_user,
                )
                proposed.action_type = action_type
                proposed.status = "proposed"
                proposed.decision = result.decision
                proposed.notes.insert(0, f"configured batch policy: {_normalize_split_brain_policy(policy)}")
                proposed.notes.insert(1, f"split-brain winner selected by policy: {selection_reason}")
                proposed.notes.insert(2, f"split-brain recovery mode: {recovery_reason}")
                return proposed
            result.notes.append(
                "interactive choices: review tied cohorts, provide keep_gfid/keep_host in a decision file, or skip"
            )
            result.notes.append(f"configured batch policy: {normalized_policy}")
            result.notes.append(
                "ambiguous file cohorts stay review-only until checksum evidence or an explicit decision file is supplied"
            )
            if error:
                result.notes.append(f"decision status: {error}")
            if winner_host:
                result.notes.append(f"heuristic winner host: {winner_host}")
            if winner_backend:
                result.notes.append(f"heuristic winner backend: {winner_backend}")
            if conflict_hosts:
                result.notes.append(f"losing/conflicting hosts: {', '.join(conflict_hosts)}")
            if mounted_target:
                result.notes.append(f"mounted target: {mounted_target}")
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "review-ambiguous-entry-split-brain"),
                    step_type="review_ambiguous_entry_split_brain",
                    target_path=mounted_target or logical_path,
                    notes=[
                        "top file-identity cohorts are tied; checksum or explicit keep_host/keep_gfid decision is required",
                        f"heuristic winner host: {winner_host or 'unknown'}",
                        f"conflict hosts: {', '.join(conflict_hosts) if conflict_hosts else 'none'}",
                    ],
                )
            )
            return result
        selected, selection_reason = _select_split_brain_file_copy(action, policy)
        if selected:
            recovery_mode, recovery_reason = _split_brain_file_recovery_mode(
                action,
                policy,
                backup_mode=backup_mode,
            )
            overridden_action = _action_with_decision_winner(action, selected)
            if recovery_mode == "quarantine_loser":
                proposed = _plan_quarantine_file_conflict(
                    overridden_action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                    quarantine_mode="quarantine_loser",
                )
                proposed.action_type = action_type
                proposed.status = "proposed"
                proposed.decision = result.decision
                proposed.notes.insert(0, f"configured batch policy: {normalized_policy}")
                proposed.notes.insert(1, f"split-brain winner selected by policy: {selection_reason}")
                proposed.notes.insert(2, f"split-brain recovery mode: {recovery_reason}")
                return proposed
            overridden_action["repair_strategy"] = "replace_entry_split_brain_file"
            proposed = _plan_repair_file(
                overridden_action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
                volume=volume,
                brick_path=brick_path,
                split_brain_native_policy=split_brain_native_policy,
                worker_path=worker_path,
                ssh_user=ssh_user,
            )
            proposed.action_type = action_type
            proposed.status = "proposed"
            proposed.decision = result.decision
            proposed.notes.insert(0, f"configured batch policy: {normalized_policy}")
            proposed.notes.insert(1, f"split-brain winner selected by policy: {selection_reason}")
            proposed.notes.insert(2, f"split-brain recovery mode: {recovery_reason}")
            return proposed
        result.notes.append(
            "interactive choices: keep for review, pick winner and replace losers, or skip"
        )
        result.notes.append(f"configured batch policy: {normalized_policy}")
        if batch and normalized_policy == "review":
            result.notes.append("batch default: keep review-only for now")
        else:
            result.notes.append(
                "interactive default suggestion: pick the likely winner and replace loser entries after review"
            )
        if winner_host:
            result.notes.append(f"likely winner host: {winner_host}")
        if winner_backend:
            result.notes.append(f"likely winner backend: {winner_backend}")
        if conflict_hosts:
            result.notes.append(f"losing/conflicting hosts: {', '.join(conflict_hosts)}")
        if mounted_target:
            result.notes.append(f"mounted target: {mounted_target}")
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, 1, "review-entry-split-brain"),
                step_type="review_entry_split_brain",
                target_path=mounted_target or logical_path,
                notes=[
                    f"likely winner host: {winner_host or 'unknown'}",
                    f"healthy hosts: {', '.join(healthy_hosts) if healthy_hosts else 'none'}",
                    f"conflict hosts: {', '.join(conflict_hosts) if conflict_hosts else 'none'}",
                    "default action: stage winner, delete loser backend/file-GFID entries, recreate once through the mount",
                ],
            )
        )
        return result

    if action_type in {"review_probable_stale_survivor", "review_probable_orphaned_symlink"}:
        if action.get("object_type") in {"directory", "directory_candidate"} or repair_strategy == "delete_below_quorum_subtree":
            policy = review_policies.get("probable_stale_survivor_directory", DEFAULT_REVIEW_POLICIES["probable_stale_survivor_directory"])
            if policy in {"auto", "delete"}:
                proposed = _plan_delete_review_directory(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
                proposed.notes.insert(0, f"configured batch policy: {policy}")
                proposed.notes.insert(1, "proposed action from review policy; inspect before execution")
                return proposed
            result.notes.append(
                "interactive choices: delete stale subtree, keep for review, or skip"
            )
            result.notes.append(f"configured batch policy: {policy}")
            if batch and policy in {"auto", "delete"}:
                result.notes.append(
                    "batch default: treat below-quorum subtree survivors as auto-delete and remove the stale subtree deepest-first"
                )
            elif batch and policy == "review":
                result.notes.append("batch default: keep review-only for now")
            elif batch and policy == "skip":
                result.notes.append("batch default: skip this review action")
            else:
                result.notes.append(
                    "interactive default suggestion: delete stale subtree remnants after review"
                )
            if healthy_hosts:
                result.notes.append(f"present hosts: {', '.join(healthy_hosts)}")
            if missing_hosts:
                result.notes.append(f"missing hosts: {', '.join(missing_hosts)}")
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "review-stale-subtree"),
                    step_type="review_probable_stale_subtree",
                    target_path=mounted_target or logical_path,
                    notes=[
                        f"present hosts: {', '.join(healthy_hosts) if healthy_hosts else 'none'}",
                        f"missing hosts: {', '.join(missing_hosts) if missing_hosts else 'none'}",
                        "default action: remove stale subtree children deepest-first, then remove the parent directory and GFID metadata",
                    ],
                )
            )
            return result
        if "file subtype: symlink" in result.notes or "saw_symlink" in result.notes or "orphaned_symlink_present" in result.notes:
            result.action_type = "review_probable_orphaned_symlink"
            result.repair_strategy = "delete_orphaned_symlink_residue"
            policy = review_policies.get(
                "probable_orphaned_symlink_file",
                DEFAULT_REVIEW_POLICIES["probable_orphaned_symlink_file"],
            )
            if policy == "delete":
                proposed = _plan_cleanup_orphaned_symlink(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
                proposed.notes.insert(0, "configured batch policy: delete")
                proposed.notes.insert(1, "proposed action from review policy; inspect before execution")
                return proposed
            result.notes.append(
                "interactive choices: delete orphaned symlink residue, keep for review, or skip"
            )
            result.notes.append(f"configured batch policy: {policy}")
            if batch and policy == "delete":
                result.notes.append(
                    "batch default: treat below-quorum orphaned symlink residues as deleted and remove backend/GFID remnants"
                )
            elif batch and policy == "review":
                result.notes.append("batch default: keep review-only for now")
            elif batch and policy == "skip":
                result.notes.append("batch default: skip this review action")
            else:
                result.notes.append(
                    "interactive default suggestion: delete orphaned symlink residue after review"
                )
            if healthy_hosts:
                result.notes.append(f"present hosts: {', '.join(healthy_hosts)}")
            if stale_hosts:
                result.notes.append(f"stale hosts: {', '.join(stale_hosts)}")
            if missing_hosts:
                result.notes.append(f"missing hosts: {', '.join(missing_hosts)}")
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "review-orphaned-symlink"),
                    step_type="review_probable_orphaned_symlink",
                    target_path=mounted_target or logical_path,
                    notes=[
                        f"present hosts: {', '.join(healthy_hosts) if healthy_hosts else 'none'}",
                        f"missing hosts: {', '.join(missing_hosts) if missing_hosts else 'none'}",
                        "default action: remove orphaned symlink backend/GFID residue, do not recreate",
                    ],
                )
            )
            return result
        policy = review_policies.get("probable_stale_survivor_file", DEFAULT_REVIEW_POLICIES["probable_stale_survivor_file"])
        if policy == "delete":
            proposed = _plan_delete_review_file(
                action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
            )
            proposed.notes.insert(0, "configured batch policy: delete")
            proposed.notes.insert(1, "proposed action from review policy; inspect before execution")
            return proposed
        if policy in {"salvage", "restore"}:
            replica_count = _stale_survivor_replica_count(action)
            if replica_count < 4:
                proposed = _plan_delete_review_file(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
                proposed.notes.insert(0, f"configured batch policy: {policy}")
                proposed.notes.insert(1, "replica-4+ only: salvage is parked for replica-3 and smaller shapes")
                proposed.notes.insert(2, "proposed action from review policy; inspect before execution")
                return proposed
            selected, selection_error = _resolve_file_decision_copy(action, result.decision)
            salvage_host = winner_host
            salvage_backend = winner_backend
            salvage_file_gfid = str(action.get("winner_file_gfid") or "")
            if selected:
                salvage_host = str(selected.get("host") or salvage_host)
                salvage_backend = str(selected.get("backend") or salvage_backend)
                salvage_file_gfid = str(
                    selected.get("backend_trusted_gfid") or selected.get("file_gfid") or salvage_file_gfid
                )
            elif result.decision and selection_error:
                result.notes.append(f"decision status: {selection_error}")
            target_backends_by_host = {
                str(host): str(path)
                for host, path in (action.get("file_target_backend_by_host") or {}).items()
                if str(host) and str(path)
            }
            unresolved_target_hosts = sorted(
                host for host in set(missing_hosts) if not target_backends_by_host.get(host)
            )
            if not salvage_host or not salvage_backend or not missing_hosts or unresolved_target_hosts:
                result.notes.append(
                    "review-first salvage is missing the canonical surviving copy or an unambiguous per-host target backend; keep the below-quorum file under review"
                )
                result.notes.append(f"configured batch policy: {policy}")
                if salvage_host:
                    result.notes.append(f"chosen source host: {salvage_host}")
                if salvage_backend:
                    result.notes.append(f"chosen source backend: {salvage_backend}")
                if healthy_hosts:
                    result.notes.append(f"present hosts: {', '.join(healthy_hosts)}")
                if stale_hosts:
                    result.notes.append(f"stale hosts: {', '.join(stale_hosts)}")
                if missing_hosts:
                    result.notes.append(f"missing hosts: {', '.join(missing_hosts)}")
                if unresolved_target_hosts:
                    result.notes.append(
                        "missing or ambiguous target backend evidence: " + ", ".join(unresolved_target_hosts)
                    )
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, 1, "review-stale-survivor"),
                        step_type="review_probable_stale_survivor",
                        target_path=mounted_target or logical_path,
                        notes=[
                            f"present hosts: {', '.join(healthy_hosts) if healthy_hosts else 'none'}",
                            f"missing hosts: {', '.join(missing_hosts) if missing_hosts else 'none'}",
                            "salvage requires a clear surviving source brick and at least one missing target host",
                        ],
                    )
                )
                return result
            result.action_type = action_type
            result.repair_strategy = "restore_below_quorum_file_from_brick"
            result.status = "proposed"
            result.notes.insert(0, f"configured batch policy: {policy}")
            result.notes.insert(1, "proposed action from review policy; inspect before execution")
            result.notes.append("replica-4+ salvage only: the direct brick-side rsync path is enabled for 4-way and larger replica sets")
            result.notes.append("review-first salvage: pull the file directly from the chosen healthy brick with rsync")
            result.notes.append(f"chosen source host: {salvage_host}")
            result.notes.append(f"chosen source backend: {salvage_backend}")
            result.notes.append(f"target hosts: {', '.join(sorted(set(missing_hosts))) if missing_hosts else 'none'}")
            if salvage_file_gfid:
                result.notes.append(f"canonical file GFID: {salvage_file_gfid}")
            index = 1
            revert_index = 1
            for host in sorted(set(missing_hosts)):
                target_backend = target_backends_by_host[host]
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, index, f"restore-file-gap-fill-{host}"),
                        step_type="restore_file_backend_gap_fill",
                        host=host,
                        source_host=salvage_host,
                        source_path=salvage_backend,
                        target_path=target_backend,
                        command_preview=rsync_brick_pull_command(
                            host,
                            salvage_host,
                            salvage_backend,
                            target_backend,
                        ),
                        notes=[
                            "target brick pulls the file with privileged brick-side rsync",
                            "this branch stays review-first and does not depend on the mount being writable",
                        ],
                    )
                )
                index += 1
                if salvage_file_gfid:
                    result.steps.append(
                        ApplyStep(
                            step_id=_step_id(action_id, index, f"attach-file-gap-fill-{host}"),
                            step_type="attach_file_gfid",
                            host=host,
                            source_path=salvage_file_gfid,
                            target_path=target_backend,
                            command_preview=_ssh_setfattr_preview(host, target_backend, salvage_file_gfid),
                            notes=[
                                "reattach the canonical file GFID after the brick-side salvage copy",
                                "this keeps the repaired brick aligned with the chosen winner",
                            ],
                        )
                    )
                    index += 1
                result.revert_steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, revert_index, f"revert-file-gap-fill-{host}"),
                        step_type="remove_stale_backend",
                        host=host,
                        target_path=target_backend,
                        tolerate_missing=True,
                        command_preview=_ssh_rm_preview(host, target_backend),
                        notes=[
                            "best-effort rollback for the brick-side salvage copy",
                            "remove the recreated backend file if the operator rejects the repair",
                        ],
                    )
                )
                revert_index += 1
            return result
        result.notes.append(
            "interactive choices: delete stale survivors, keep for review, or skip"
        )
        result.notes.append(f"configured batch policy: {policy}")
        if batch and policy == "delete":
            result.notes.append(
                "batch default: treat below-quorum survivors as deleted and remove backend/GFID remnants"
            )
        elif batch and policy == "review":
            result.notes.append("batch default: keep review-only for now")
        elif batch and policy == "skip":
            result.notes.append("batch default: skip this review action")
        else:
            result.notes.append(
                "interactive default suggestion: delete stale survivor remnants after review"
            )
        if healthy_hosts:
            result.notes.append(f"present hosts: {', '.join(healthy_hosts)}")
        if stale_hosts:
            result.notes.append(f"stale hosts: {', '.join(stale_hosts)}")
        if missing_hosts:
            result.notes.append(f"missing hosts: {', '.join(missing_hosts)}")
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, 1, "review-stale-survivor"),
                step_type="review_probable_stale_survivor",
                target_path=mounted_target or logical_path,
                notes=[
                    f"present hosts: {', '.join(healthy_hosts) if healthy_hosts else 'none'}",
                    f"missing hosts: {', '.join(missing_hosts) if missing_hosts else 'none'}",
                    "default action: remove stale backend subtree/file and GFID metadata, do not recreate",
                ],
            )
        )
        return result

    if action_type == "cleanup_orphaned_symlink":
        result.notes.append("interactive choices: delete orphaned symlink residue, keep for review, or skip")
        result.notes.append("configured batch policy: delete")
        result.notes.append("proposed cleanup action from review policy; inspect before execution")
        if healthy_hosts:
            result.notes.append(f"present hosts: {', '.join(healthy_hosts)}")
        if stale_hosts:
            result.notes.append(f"stale hosts: {', '.join(stale_hosts)}")
        if missing_hosts:
            result.notes.append(f"missing hosts: {', '.join(missing_hosts)}")
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "cleanup-orphaned-symlink"),
                    step_type="cleanup_orphaned_symlink",
                    target_path=mounted_target or logical_path,
                    notes=[
                        f"present hosts: {', '.join(healthy_hosts) if healthy_hosts else 'none'}",
                        f"missing hosts: {', '.join(missing_hosts) if missing_hosts else 'none'}",
                        "default action: remove orphaned symlink backend/GFID residue, do not recreate",
                    ],
                )
            )
            return result

    if action_type == "review_directory_gfid_conflict":
        policy = review_policies.get("directory_gfid_conflict", DEFAULT_REVIEW_POLICIES["directory_gfid_conflict"])
        directory_presence_tie = repair_strategy == "ambiguous_directory_presence_tie"
        merge_safe = _directory_merge_is_safe(action) and not directory_presence_tie
        if directory_presence_tie:
            result.notes.append(
                "exactly half of the replicas carry the directory; auto restore, subtree deletion, and merge are disabled because the visible cohort has no strict-majority authority"
            )
            result.notes.append(
                "recommended next step: quarantine all visible directory trees, then make an explicit restore-or-delete decision from preserved evidence"
            )
        if directory_presence_tie:
            result.notes.append(
                "interactive choices: quarantine all visible directory trees, keep for review, or skip"
            )
            result.notes.append(
                "preferred choice: quarantine_both, followed by an explicit restore-or-delete decision"
            )
        else:
            result.notes.append(
                "interactive choices: merge shared directory contents, quarantine one or both directory trees, keep for review, or skip"
            )
            result.notes.append(
                "preferred choices: merge, quarantine_both, or quarantine_loser; legacy keep/prune selectors remain review-only"
            )
            if result.recommended_choice:
                result.notes.append(
                    f"recommended action: {result.recommended_choice}; {result.recommended_reason}"
                )
        result.notes.append(f"configured batch policy: {policy}")
        if batch and policy == "review":
            result.notes.append(
                "batch default: keep review-only for now; do not auto-quarantine conflicting directory trees"
            )
        elif batch and policy in {"quarantine", "rename"}:
            result.notes.append(
                "batch default: quarantine both directory trees so the operator can inspect the divergent histories"
                if policy == "quarantine"
                else "batch default: legacy rename policy maps to quarantine both directory trees"
            )
        elif batch and policy == "merge":
            if merge_safe:
                result.notes.append(
                    "batch default: merge is safe here; auto-promote to the canonical directory GFID attach path"
                )
            else:
                result.notes.append(
                    "batch default: merge requested, but the bounded trees do not yet collapse cleanly; keep review-only"
                )
        elif batch and policy == "skip":
            result.notes.append("batch default: skip this review action")
        else:
            result.notes.append(
                "interactive default suggestion: merge only after explicit review, or quarantine one or both directory trees"
            )
        if result.decision:
            result.notes.append(f"decision file entry loaded: {json.dumps(result.decision, sort_keys=True)}")
            directory_choice = _directory_tie_decision_choice(result.decision)
            if directory_choice:
                result.notes.append(f"directory tie decision: {directory_choice}")
            recommended_choice = str(result.decision.get("recommended_choice") or "").strip()
            if recommended_choice:
                result.notes.append(f"directory tie recommended choice: {recommended_choice}")
            effective_choice = _directory_tie_effective_choice(result.decision)
            if directory_choice == "auto" and effective_choice and effective_choice != "auto":
                result.notes.append(f"directory tie auto resolved to: {effective_choice}")
            if effective_choice in {
                "quarantine_both",
                "quarantine_loser",
                "quarantine_side",
            }:
                proposed = _plan_quarantine_directory_conflict(
                    action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                    quarantine_mode=effective_choice,
                    quarantine_identity=str(
                        result.decision.get("directory_quarantine_identity")
                        or ""
                    ),
                )
                if proposed.status == "review":
                    return proposed
                proposed.notes.insert(0, f"configured batch policy: {policy}")
                proposed.notes.insert(1, "proposed action from explicit decision file; inspect before execution")
                proposed.notes.insert(2, f"directory tie decision: {directory_choice}")
                if recommended_choice:
                    proposed.notes.insert(3, f"directory tie recommended choice: {recommended_choice}")
                if directory_choice == "auto" and effective_choice and effective_choice != "auto":
                    proposed.notes.insert(4, f"directory tie auto resolved to: {effective_choice}")
                proposed.notes.append(
                    "directory tie quarantine resolved to the reversible quarantine path; keep the operator report for later inspection"
                )
                proposed.decision = result.decision
                return proposed
            if effective_choice == "merge" and merge_safe:
                promoted_action = dict(action)
                promoted_action["action_type"] = "repair_directory_metadata"
                promoted_action["repair_strategy"] = "attach_directory_gfid"
                promoted_action["directory_metadata_mismatch_hosts"] = _directory_conflict_mismatch_hosts(action)
                proposed = _plan_reconcile_directory(
                    promoted_action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
                proposed.status = "proposed"
                proposed.decision = result.decision
                proposed.notes.insert(0, f"configured batch policy: {policy}")
                proposed.notes.insert(1, "proposed action from explicit decision file; inspect before execution")
                proposed.notes.insert(2, f"directory tie decision: {directory_choice}")
                if recommended_choice:
                    proposed.notes.insert(3, f"directory tie recommended choice: {recommended_choice}")
                if directory_choice == "auto" and effective_choice and effective_choice != "auto":
                    proposed.notes.insert(4, f"directory tie auto resolved to: {effective_choice}")
                proposed.notes.append(
                    "directory tie merge resolved to the existing attach_directory_gfid path; this repairs identity without recreating the directory tree"
                )
                return proposed
            if effective_choice and effective_choice not in {"defer", "keep_review", "merge"}:
                result.status = "proposed"
                result.notes.append("proposed action from explicit decision file; inspect before execution")
            if effective_choice == "merge" and not merge_safe:
                result.notes.append(
                    "merge requires a shared child signature and a canonical directory backend; keep this conflict under review"
                )
        elif batch and policy in {"quarantine", "rename"}:
            proposed = _plan_quarantine_directory_conflict(
                action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
                quarantine_mode="quarantine_both",
            )
            proposed.notes.insert(0, f"configured batch policy: {policy}")
            proposed.notes.insert(1, "proposed action from matrix default; inspect before execution")
            proposed.notes.append(
                "directory tie quarantine resolves to a reversible quarantine of both directory trees"
            )
            return proposed
        elif batch and policy == "merge" and merge_safe:
            promoted_action = dict(action)
            promoted_action["action_type"] = "repair_directory_metadata"
            promoted_action["repair_strategy"] = "attach_directory_gfid"
            promoted_action["directory_metadata_mismatch_hosts"] = _directory_conflict_mismatch_hosts(action)
            proposed = _plan_reconcile_directory(
                promoted_action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
            )
            proposed.status = "proposed"
            proposed.notes.insert(0, f"configured batch policy: {policy}")
            proposed.notes.insert(1, "proposed action from matrix default; inspect before execution")
            proposed.notes.append(
                "directory tie merge resolved to the existing attach_directory_gfid path; this repairs identity without recreating the directory tree"
            )
            return proposed
        if healthy_hosts:
            result.notes.append(f"present hosts: {', '.join(healthy_hosts)}")
        if missing_hosts:
            result.notes.append(f"missing hosts: {', '.join(missing_hosts)}")
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, 1, "review-directory-gfid-conflict"),
                step_type="review_directory_gfid_conflict",
                target_path=mounted_target or logical_path,
                notes=[
                    f"present hosts: {', '.join(healthy_hosts) if healthy_hosts else 'none'}",
                    f"missing hosts: {', '.join(missing_hosts) if missing_hosts else 'none'}",
                    "default action: keep the conflict review-only until the operator chooses merge or quarantine",
                ],
            )
        )
        return result

    if action_type == "review_type_mismatch":
        policy = review_policies.get("type_mismatch", DEFAULT_REVIEW_POLICIES["type_mismatch"])
        decision_choice = str(
            result.decision.get("type_mismatch_choice")
            or result.decision.get("choice")
            or ""
        ).strip().replace("-", "_").replace(" ", "_").lower()
        if decision_choice in {"quarantine_both", "quarantine_loser"}:
            proposed = _plan_quarantine_type_mismatch(
                action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
                quarantine_mode=decision_choice,
            )
            proposed.decision = result.decision
            proposed.notes.insert(0, "proposed action from explicit guided type-mismatch decision; inspect before execution")
            proposed.notes.insert(
                1,
                "guided choice: quarantine both file and directory branches"
                if decision_choice == "quarantine_both"
                else "guided choice: quarantine the complete older type branch",
            )
            return proposed
        if policy in {"quarantine-both", "quarantine-loser"}:
            quarantine_mode = policy.replace("-", "_")
            proposed = _plan_quarantine_type_mismatch(
                action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
                quarantine_mode=quarantine_mode,
            )
            proposed.notes.insert(0, f"configured batch policy: {policy}")
            proposed.notes.insert(1, "proposed reversible action from explicit type-mismatch policy; inspect before execution")
            return proposed
        recommended_choice = str(action.get("recommended_choice") or "").strip().replace("-", "_").replace(" ", "_").lower()
        if recommended_choice not in {"quarantine_loser", "quarantine_both"}:
            recommended_choice = "quarantine_both"
        recommended_reason = str(action.get("recommended_reason") or "").strip()
        if not recommended_reason:
            recommended_reason = (
                "Backend mtime evidence clearly separates the file and directory sides; quarantine the older branch."
                if recommended_choice == "quarantine_loser"
                else "Backend mtime evidence is incomplete or overlapping; quarantine both branches first."
            )
        result.recommended_choice = recommended_choice
        result.recommended_reason = recommended_reason

        def _append_unique(note: str) -> None:
            if note and note not in result.notes:
                result.notes.append(note)

        _append_unique("interactive choices: quarantine the older branch, quarantine both branches, keep review, or skip")
        _append_unique("configured batch policy: review")
        _append_unique(f"type-mismatch recommended choice: {recommended_choice}")
        _append_unique(f"type-mismatch recommended reason: {recommended_reason}")
        _append_unique(
            "interactive default suggestion: quarantine the older branch after reviewing the backend mtime split"
            if recommended_choice == "quarantine_loser"
            else "interactive default suggestion: quarantine both branches first; only pick a source after a fresh report proves one side authoritative"
        )
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, 1, "review-type-mismatch"),
                step_type="review_type_mismatch",
                target_path=mounted_target or logical_path,
                notes=[
                    "file-vs-directory disagreement is too risky to auto-resolve",
                    (
                        "default operator action: quarantine the older branch"
                        if recommended_choice == "quarantine_loser"
                        else "default operator action: quarantine both branches first"
                    ),
                ],
            )
        )
        return result

    if action_type == "review_directory_children":
        policy = review_policies.get("directory_children", DEFAULT_REVIEW_POLICIES["directory_children"])
        child_gap_executable = bool(action.get("depends_on")) and (
            "directory_child_gap:executable" in (action.get("graph_markers") or [])
        )
        result.notes.append(
            "interactive choices: keep for review, apply the child-gap suggestion, or skip"
        )
        result.notes.append(f"configured batch policy: {policy}")
        if repair_strategy == "restore_directory_children_bounded" and policy == "auto":
            promoted_action = dict(action)
            promoted_action["action_type"] = "reconcile_directory"
            promoted_action["repair_strategy"] = "restore_directory_children_bounded"
            proposed = _plan_reconcile_directory(
                promoted_action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
            )
            proposed.status = "proposed"
            proposed.notes.insert(0, "configured batch policy: auto")
            proposed.notes.insert(1, "proposed action from bounded subtree candidate; inspect before execution")
            proposed.notes.append(
                "bounded subtree candidate promoted to the executable leaf-first restore path"
            )
            return proposed
        if policy == "auto" and child_gap_executable:
            result.notes.append(
                "batch default: auto-promote the proven child dependencies, then heal/rescan the parent"
            )
        else:
            result.notes.append(
                "batch default: keep review-only; repair the child set first, then rerun manifest-build and plan-build so the parent can be reconsidered"
            )
        if result.decision:
            result.notes.append(f"decision file entry loaded: {json.dumps(result.decision, sort_keys=True)}")
            directory_choice = _directory_tie_decision_choice(result.decision)
            if directory_choice:
                result.notes.append(f"directory tie decision: {directory_choice}")
            recommended_choice = str(result.decision.get("recommended_choice") or "").strip()
            if recommended_choice:
                result.notes.append(f"directory tie recommended choice: {recommended_choice}")
            effective_choice = _directory_tie_effective_choice(result.decision)
            if directory_choice == "auto" and effective_choice and effective_choice != "auto":
                result.notes.append(f"directory tie auto resolved to: {effective_choice}")
            if effective_choice == "reconcile_directory_children" and child_gap_executable:
                promoted_action = dict(action)
                promoted_action["action_type"] = "reconcile_directory"
                promoted_action["repair_strategy"] = "reconcile_directory_children"
                proposed = _plan_reconcile_directory(
                    promoted_action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
                proposed.decision = result.decision
                proposed.notes.insert(0, "configured batch policy: review")
                proposed.notes.insert(1, "proposed action from explicit decision file; inspect before execution")
                proposed.notes.insert(2, f"directory tie decision: {directory_choice}")
                if recommended_choice:
                    proposed.notes.insert(3, f"directory tie recommended choice: {recommended_choice}")
                if directory_choice == "auto" and effective_choice and effective_choice != "auto":
                    proposed.notes.insert(4, f"directory tie auto resolved to: {effective_choice}")
                return proposed
            if effective_choice == "reconcile_directory_children":
                result.notes.append(
                    "directory-child reconciliation was selected, but no executable child dependency is attached; collect immediate GFID-child evidence first"
                )
            if (
                effective_choice
                and effective_choice not in {"defer", "keep_review", "reconcile_directory_children"}
                and not (
                    effective_choice == "repair_directory_metadata"
                    and repair_strategy == "review_directory_mdata_state"
                )
            ):
                result.status = "proposed"
        elif policy == "auto" and child_gap_executable:
            promoted_action = dict(action)
            promoted_action["action_type"] = "reconcile_directory"
            promoted_action["repair_strategy"] = "reconcile_directory_children"
            proposed = _plan_reconcile_directory(
                promoted_action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
            )
            proposed.status = "proposed"
            proposed.notes.insert(0, "configured batch policy: auto")
            proposed.notes.insert(1, "proposed action from matrix default; inspect before execution")
            return proposed
        child_names_by_host = action.get("directory_child_names_by_host") or {}
        missing_children_by_host = action.get("missing_directory_children_by_host") or {}
        if child_names_by_host:
            result.notes.append(
                "child sets by host: "
                + ", ".join(
                    f"{host}=[{', '.join(names)}]" for host, names in sorted(child_names_by_host.items())
                )
            )
        if missing_children_by_host:
            result.notes.append(
                "missing children by host: "
                + ", ".join(
                    f"{host}=[{', '.join(names)}]" for host, names in sorted(missing_children_by_host.items()) if names
                )
            )
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, 1, "review-directory-children"),
                step_type="review_directory_children",
                target_path=mounted_target or logical_path,
                notes=[
                    "immediate children differ across bricks; inspect the child set on a healthy brick before changing the parent directory",
                    "do not treat this as a plain metadata-only case",
                ],
            )
        )
        return result

    if action_type == "review_directory_metadata":
        policy = review_policies.get("directory_metadata", DEFAULT_REVIEW_POLICIES["directory_metadata"])
        result.notes.append(
            "interactive choices: keep for review or skip"
        )
        result.notes.append(f"configured batch policy: {policy}")
        result.notes.append(
            "batch default: keep review-only; no automatic directory metadata action"
            if policy == "review"
            else "batch default: skip this review action"
        )
        if result.decision:
            result.notes.append(f"decision file entry loaded: {json.dumps(result.decision, sort_keys=True)}")
            directory_choice = _directory_tie_decision_choice(result.decision)
            if directory_choice:
                result.notes.append(f"directory tie decision: {directory_choice}")
            recommended_choice = str(result.decision.get("recommended_choice") or "").strip()
            if recommended_choice:
                result.notes.append(f"directory tie recommended choice: {recommended_choice}")
            effective_choice = _directory_tie_effective_choice(result.decision)
            if directory_choice == "auto" and effective_choice and effective_choice != "auto":
                result.notes.append(f"directory tie auto resolved to: {effective_choice}")

            if repair_strategy == "review_directory_mdata_state" and effective_choice in {
                "repair_directory_mdata",
                "attach_directory_mdata",
                "repair_directory_metadata",
            }:
                promoted_action, decision_error = _directory_mdata_source_decision(action, result.decision)
                if promoted_action:
                    proposed = _plan_reconcile_directory(
                        promoted_action,
                        execution_mode=execution_mode,
                        backup_root=backup_root,
                        backup_mode=backup_mode,
                        batch=batch,
                    )
                    proposed.status = "proposed"
                    proposed.decision = result.decision
                    proposed.notes.insert(0, "configured batch policy: review")
                    proposed.notes.insert(1, "proposed action from explicit directory mdata source decision; inspect before execution")
                    proposed.notes.insert(2, f"directory tie decision: {directory_choice}")
                    if recommended_choice:
                        proposed.notes.insert(3, f"directory tie recommended choice: {recommended_choice}")
                    if directory_choice == "auto" and effective_choice and effective_choice != "auto":
                        proposed.notes.insert(4, f"directory tie auto resolved to: {effective_choice}")
                    return proposed
                result.notes.append(f"decision status: {decision_error}")
            if effective_choice == "repair_directory_metadata" and repair_strategy != "review_directory_mdata_state":
                promoted_action = dict(action)
                promoted_action["action_type"] = "repair_directory_metadata"
                promoted_action["repair_strategy"] = "attach_directory_gfid"
                proposed = _plan_reconcile_directory(
                    promoted_action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
                proposed.status = "proposed"
                proposed.decision = result.decision
                proposed.notes.insert(0, "configured batch policy: review")
                proposed.notes.insert(1, "proposed action from explicit decision file; inspect before execution")
                proposed.notes.insert(2, f"directory tie decision: {directory_choice}")
                if recommended_choice:
                    proposed.notes.insert(3, f"directory tie recommended choice: {recommended_choice}")
                if directory_choice == "auto" and effective_choice and effective_choice != "auto":
                    proposed.notes.insert(4, f"directory tie auto resolved to: {effective_choice}")
                return proposed
            if effective_choice == "repair_directory_metadata" and repair_strategy == "review_directory_mdata_state":
                result.notes.append(
                    "directory mdata review has no clear majority; repair requires an explicit trusted.glusterfs.mdata source value, not the GFID attach branch"
                )
            if (
                effective_choice
                and effective_choice not in {"defer", "keep_review"}
                and not (
                    repair_strategy == "review_directory_mdata_state"
                    and effective_choice in {
                        "repair_directory_mdata",
                        "attach_directory_mdata",
                        "repair_directory_metadata",
                    }
                )

            ):
                result.status = "proposed"
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, 1, "review-directory-metadata"),
                step_type="review_directory_metadata",
                target_path=mounted_target or logical_path,
                notes=["default action: review-only directory metadata case with no automatic execution policy"],
            )
        )
        return result

    if action_type == "review_file_metadata":
        policy = review_policies.get("file_metadata_only", DEFAULT_REVIEW_POLICIES["file_metadata_only"])
        result.notes.append(
            "interactive choices: repair metadata drift, keep for review, or skip"
        )
        result.notes.append(f"configured batch policy: {policy}")
        symlink_residue = "orphaned_symlink_present" in result.notes
        mismatch_hosts = _file_metadata_mismatch_hosts_from_action(action)
        canonical_gfid = str(action.get("winner_file_gfid") or "")
        canonical_backend = str(action.get("winner_backend") or "")
        if symlink_residue:
            promoted_action = dict(action)
            promoted_action["action_type"] = "review_probable_orphaned_symlink"
            promoted_action["repair_strategy"] = "delete_orphaned_symlink_residue"
            orphaned_policy = review_policies.get(
                "probable_orphaned_symlink_file",
                DEFAULT_REVIEW_POLICIES["probable_orphaned_symlink_file"],
            )
            if orphaned_policy == "delete":
                proposed = _plan_cleanup_orphaned_symlink(
                    promoted_action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                )
                proposed.notes.insert(0, "configured batch policy: delete")
                proposed.notes.insert(
                    1,
                    "promoted from file-metadata review because the surviving object is orphaned symlink residue",
                )
                return proposed
            result.action_type = "review_probable_orphaned_symlink"
            result.repair_strategy = "delete_orphaned_symlink_residue"
            result.notes.append(
                "interactive choices: delete orphaned symlink residue, keep for review, or skip"
            )
            result.notes.append(f"configured batch policy: {orphaned_policy}")
            result.notes.append(
                "batch default: delete orphaned symlink residue when the symlink inode survives below quorum"
                if orphaned_policy == "delete"
                else "batch default: keep review-only for now"
            )
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "review-orphaned-symlink"),
                    step_type="review_probable_orphaned_symlink",
                    target_path=mounted_target or logical_path,
                    notes=[
                        "symlink inode survives, but the target is not a live Gluster terminal object",
                        "default action: back up and delete the orphaned symlink residue when policy allows",
                    ],
                )
            )
            return result
        if policy == "repair" and mismatch_hosts and canonical_gfid and canonical_backend:
            promoted_action = dict(action)
            promoted_action["action_type"] = "repair_file_metadata"
            promoted_action["repair_strategy"] = "attach_file_gfid"
            promoted_action["file_metadata_mismatch_hosts"] = mismatch_hosts
            proposed = _plan_repair_file_metadata(
                promoted_action,
                execution_mode=execution_mode,
                backup_root=backup_root,
                backup_mode=backup_mode,
                batch=batch,
            )
            proposed.notes.insert(0, "configured batch policy: repair")
            proposed.notes.insert(1, "proposed action from review policy; inspect before execution")
            return proposed
        if policy == "repair":
            result.notes.append(
                "batch default: keep review-only; file-metadata repair needs canonical GFID evidence and mismatch hosts"
            )
        elif policy == "skip":
            result.notes.append("batch default: skip this review action")
        else:
            result.notes.append("batch default: keep review-only for now")
        if mismatch_hosts:
            result.notes.append(f"metadata mismatch hosts: {', '.join(mismatch_hosts)}")
        if canonical_gfid:
            result.notes.append(f"canonical file GFID: {canonical_gfid}")
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, 1, "review-file-metadata"),
                step_type="review_file_metadata",
                target_path=mounted_target or logical_path,
                notes=[
                    "default action: keep the file metadata case under review unless policy and evidence allow attach_file_gfid",
                ],
            )
        )
        return result

    if action_type == "review_posix_metadata_no_majority":
        if result.decision:
            result.notes.append(f"decision file entry loaded: {json.dumps(result.decision, sort_keys=True)}")
            promoted_action, decision_error = _posix_metadata_source_decision(action, result.decision)
            if promoted_action:
                proposed = _plan_repair_posix_metadata(
                    promoted_action,
                    execution_mode=execution_mode,
                    backup_root=backup_root,
                    backup_mode=backup_mode,
                    batch=batch,
                    volume=volume,
                    brick_path=brick_path,
                    ssh_user=ssh_user,
                )
                proposed.decision = result.decision
                proposed.notes.insert(0, "proposed action from explicit POSIX metadata source decision; inspect before execution")
                return proposed
            result.notes.append(f"decision status: {decision_error}")
        result.notes.append(
            "interactive choices: choose source tuple, keep review, or skip"
        )
        result.notes.append(
            f"configured batch policy: {review_policies.get('posix_metadata_no_majority', DEFAULT_REVIEW_POLICIES['posix_metadata_no_majority'])}"
        )
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, 1, "review-posix-metadata-no-majority"),
                step_type="review_posix_metadata_no_majority",
                target_path=mounted_target or logical_path,
                notes=[
                    "POSIX mode/uid/gid/ACL values differ without a strict majority",
                    "choose a source host/value explicitly before promoting to repair_posix_metadata",
                ],
            )
        )
        return result

    if action_type == "repair_directory_metadata":
        mismatch_hosts = [
            str(host)
            for host in (
                action.get("directory_mdata_mismatch_hosts")
                if repair_strategy == "attach_directory_mdata"
                else action.get("directory_metadata_mismatch_hosts")
            )
            or []
            if str(host)
        ]
        canonical_gfid = str(action.get("directory_canonical_gfid") or "")
        canonical_mdata = str(action.get("directory_canonical_mdata_hex") or "")
        canonical_backend = str(action.get("directory_canonical_backend") or "")
        backend_by_host = {
            str(host): str(path)
            for host, path in (action.get("directory_backend_by_host") or {}).items()
            if str(host) and str(path)
        }
        if repair_strategy == "attach_directory_mdata" and canonical_mdata and canonical_backend and mismatch_hosts:
            result.status = "proposed"
            result.notes.append("directory mdata repair is a trusted.glusterfs.mdata majority align, not a GFID attach")
            result.notes.append(
                f"canonical directory mdata: {canonical_mdata}; target backend: {canonical_backend}"
            )
            result.notes.append(f"mdata mismatch hosts: {', '.join(mismatch_hosts)}")
            for host in mismatch_hosts:
                target_backend = backend_by_host.get(host, canonical_backend)
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, len(result.steps) + 1, f"attach-dir-mdata-{host}"),
                        step_type="attach_directory_mdata",
                        host=host,
                        source_path=canonical_mdata,
                        target_path=target_backend,
                        command_preview=_ssh_set_mdata_preview(host, target_backend, canonical_mdata),
                        notes=[
                            "align trusted.glusterfs.mdata to the clear majority value",
                            "this repairs directory ctime/mdata drift without touching trusted.gfid or payload children",
                        ],
                    )
                )
            return result
        if canonical_gfid and canonical_backend and mismatch_hosts:
            result.status = "proposed"
            result.notes.append("directory metadata repair is a GFID xattr attach, not a mkdir")
            result.notes.append(
                f"canonical directory GFID: {canonical_gfid}; target backend: {canonical_backend}"
            )
            result.notes.append(f"metadata mismatch hosts: {', '.join(mismatch_hosts)}")
            for host in mismatch_hosts:
                result.steps.append(
                    ApplyStep(
                        step_id=_step_id(action_id, len(result.steps) + 1, f"attach-dir-metadata-{host}"),
                        step_type="attach_directory_gfid",
                        host=host,
                        source_path=canonical_gfid,
                        target_path=canonical_backend,
                        command_preview=_ssh_setfattr_preview(host, canonical_backend, canonical_gfid),
                        notes=[
                            "reattach the canonical directory GFID xattr to the existing backend directory",
                            "this repairs metadata drift without recreating the directory tree",
                        ],
                    )
                )
            return result
        result.action_type = "review_directory_metadata"
        result.notes.append("directory metadata repair could not be proven safely; keep review-only")
        return result

    if action_type == "review_dead_gfid_reference":
        if repair_strategy == "refresh_stale_index_evidence":
            result.notes.append(
                "interactive choices: refresh read-only index evidence, keep for review, or skip"
            )
            result.notes.append("configured batch policy: review")
            result.notes.append(
                "batch default: keep review-only; do not delete until an exact index path is observed on a brick"
            )
            result.steps.append(
                ApplyStep(
                    step_id=_step_id(action_id, 1, "review-stale-index-evidence"),
                    step_type="review_stale_glusterfs_index_cleanup",
                    target_path=mounted_target or logical_path,
                    notes=[
                        "no exact stale index path was observed on a brick",
                        "default action: refresh read-only index evidence before any cleanup decision",
                    ],
                )
            )
            return result
        live_refs = [
            str(ref)
            for ref in action.get("dead_gfid_live_references") or []
            if str(ref)
        ]
        result.notes.append(
            "interactive choices: inspect the live reference, keep for review, or skip"
        )
        result.notes.append("configured batch policy: review")
        result.notes.append(
            "batch default: keep review-only; confirm whether the GFID still resolves to a live terminal file or directory before deleting the residue"
        )
        if live_refs:
            result.notes.append(f"live reference targets: {', '.join(live_refs)}")
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, 1, "review-dead-gfid-reference"),
                step_type="review_dead_gfid_reference",
                target_path=mounted_target or logical_path,
                notes=[
                    f"live references: {', '.join(live_refs) if live_refs else 'unknown'}",
                    "default action: follow the live reference chain to the terminal file or directory before deciding whether the GFID residue is stale",
                ],
            )
        )
        return result

    result.steps.append(
        ApplyStep(
            step_id=_step_id(action_id, 1, "review-generic"),
            step_type="review_action",
            target_path=mounted_target or logical_path,
            notes=["review-only action with no automatic execution policy yet"],
        )
    )
    return result
