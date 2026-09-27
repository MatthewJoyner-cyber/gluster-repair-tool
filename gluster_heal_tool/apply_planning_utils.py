# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Shared utility helpers for Gluster repair apply planning."""
from __future__ import annotations

import base64
import shlex
from pathlib import Path
from posixpath import dirname

from .apply import DEFAULT_SPLIT_BRAIN_NATIVE_POLICY
from .controller_paths import default_stage_local_path
from .install_paths import DEFAULT_SERVICE_USER
from .models import ApplyActionResult, ApplyStep
from .remote_ops import rsync_pull_command, rsync_push_command, ssh_remote_command


def _step_id(action_id: str, index: int, label: str) -> str:
    return f"{action_id}:{index:02d}:{label}"


def _safe_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value).strip())
    return token or "unknown"


def _ssh_rm_preview(host: str, path: str) -> list[str]:
    return ssh_remote_command(host, ["rm", "-f", "--", path])


def _ssh_rmr_preview(host: str, path: str) -> list[str]:
    return ssh_remote_command(host, ["rm", "-rf", "--", path])


def _stage_preview(host: str, source: str, target: str, *, preserve_symlink: bool = False) -> list[str]:
    # Use rsync for staging so symlink-backed winners are copied as links, not dereferenced targets.
    command = rsync_pull_command(host, source, target)
    # Preserve numeric ownership as well as ACLs/user xattrs. An unprivileged
    # receiver silently changes ownership to the controller user. Gluster's
    # trusted.* backend attributes must not be copied into scratch or a mount.
    command[1:2] = ["-aAX", "--numeric-ids", "--filter=-x! user.*"]
    return ["sudo", "-n", *command]


def _rsync_directory_contents_path(path: str) -> str:
    stripped = str(path).rstrip("/")
    if not stripped:
        return "./"
    if stripped == "/":
        return "/"
    return f"{stripped}/"


def _stage_tree_contents_preview(host: str, source: str, target: str) -> list[str]:
    # Stage scratch data as directory contents, not as a nested basename; --delete is
    # safe here because the target is tool-owned scratch space.
    command = rsync_pull_command(
        host,
        _rsync_directory_contents_path(source),
        _rsync_directory_contents_path(target),
        delete=True,
    )
    command[1:2] = ["-aAX", "--numeric-ids", "--filter=-x! user.*"]
    return ["sudo", "-n", *command]


def _push_preview(host: str, source: str, target: str) -> list[str]:
    return rsync_push_command(host, source, target)


def _push_tree_contents_preview(host: str, source: str, target: str) -> list[str]:
    # Push the staged subtree contents into the existing backend directory. Without
    # trailing slashes rsync creates target/basename, which leaves child gaps intact.
    command = rsync_push_command(
        host,
        _rsync_directory_contents_path(source),
        _rsync_directory_contents_path(target),
    )
    command[1:2] = ["-aAX", "--numeric-ids", "--filter=-x! user.*"]
    return ["sudo", "-n", *command]


def _restore_preview(source: str, target: str, *, preserve_symlink: bool = False) -> list[str]:
    return ["sudo", "-n", "cp", "-a", source, target]


def _mount_restore_preview(source: str, target: str, *, preserve_symlink: bool = False) -> list[str]:
    return ["sudo", "-n", "cp", "-a", source, target]


def _mount_tree_restore_preview(source: str, target: str) -> list[str]:
    source_tree = source.rstrip("/") or "."
    return ["sudo", "-n", "cp", "-a", f"{source_tree}/.", target]


def _mkdir_preview(path: str) -> list[str]:
    return ["mkdir", "-p", path]


def _ssh_cp_preview(host: str, source: str, target: str) -> list[str]:
    # Use archive mode so backup/revert steps can handle files, directories, and symlinks safely.
    return ssh_remote_command(host, ["cp", "-a", "--", source, target])


def _ssh_mv_preview(host: str, source: str, target: str) -> list[str]:
    return ssh_remote_command(host, ["mv", "--", source, target])


def _directory_quarantine_target(
    source: str,
    *,
    action_id: str,
    host: str,
    identity: str,
    mode: str,
) -> str:
    base = source.rstrip("/") or "/"
    return (
        f"{base}.gluster-quarantine."
        f"{_safe_token(mode)}."
        f"{_safe_token(action_id)}."
        f"{_safe_token(host)}."
        f"{_safe_token(identity)[:8]}"
    )


def _file_quarantine_target(
    source: str,
    *,
    action_id: str,
    host: str,
    identity: str,
    mode: str,
) -> str:
    base = source.rstrip("/") or "/"
    return (
        f"{base}.gluster-quarantine."
        f"{_safe_token(mode)}."
        f"{_safe_token(action_id)}."
        f"{_safe_token(host)}."
        f"{_safe_token(identity)[:8]}"
    )


def _local_rm_preview(path: str) -> list[str]:
    return ["sudo", "-n", "rm", "-f", "--", path]


def _mount_mkdir_preview(path: str) -> list[str]:
    return ["sudo", "-n", "mkdir", "-p", path]


def _ssh_mkdir_preview(host: str, path: str) -> list[str]:
    return ssh_remote_command(host, ["mkdir", "-p", "--", path])


def _ssh_setfattr_preview(host: str, path: str, gfid: str) -> list[str]:
    cleaned = gfid.replace("-", "").strip()
    if cleaned.startswith(("0x", "0X")):
        cleaned = cleaned[2:]
    raw = base64.b64encode(bytes.fromhex(cleaned)).decode("ascii")
    return ssh_remote_command(host, ["setfattr", "-n", "trusted.gfid", "-v", f"0s{raw}", "--", path])


def _ssh_relink_gfid_preview(host: str, backend_root: str, target: str, *, directory: bool) -> list[str]:
    command = "link-directory-gfid" if directory else "link-file-gfid"
    return ssh_remote_command(
        host,
        [command, "--backend-root", backend_root, "--target", target],
    )


def _ssh_chmod_numeric_preview(
    host: str,
    mode_bits: int,
    target: str,
    *,
    ssh_user: str = DEFAULT_SERVICE_USER,
) -> list[str]:
    return ssh_remote_command(host, ["chmod", f"{mode_bits:04o}", "--", target], ssh_user=ssh_user)


def _ssh_chown_numeric_preview(
    host: str,
    uid: int,
    gid: int,
    target: str,
    *,
    ssh_user: str = DEFAULT_SERVICE_USER,
) -> list[str]:
    return ssh_remote_command(host, ["chown", f"{uid}:{gid}", "--", target], ssh_user=ssh_user)


def _ssh_acl_reference_preview(
    source_host: str,
    source_path: str,
    target_host: str,
    target_path: str,
    *,
    ssh_user: str = DEFAULT_SERVICE_USER,
    clear_default: bool = False,
) -> list[str]:
    source_cmd = shlex.join(
        ssh_remote_command(
            source_host,
            ["getfacl", "-c", "--absolute-names", "--", source_path],
            ssh_user=ssh_user,
        )
    )
    target_cmd = shlex.join(
        ssh_remote_command(
            target_host,
            ["setfacl", "--set-file=-", "--", target_path],
            ssh_user=ssh_user,
        )
    )
    pipeline = f"{source_cmd} | {target_cmd}"
    if clear_default:
        clear_cmd = shlex.join(
            ssh_remote_command(
                target_host,
                ["setfacl", "-k", "--", target_path],
                ssh_user=ssh_user,
            )
        )
        pipeline = f"{clear_cmd} && {pipeline}"
    # A successful target command must not hide a failed source ACL read.
    return ["bash", "-o", "pipefail", "-c", pipeline]


def _ssh_set_mdata_preview(host: str, path: str, mdata_hex: str) -> list[str]:
    cleaned = mdata_hex.strip()
    if cleaned.startswith(("0x", "0X")):
        cleaned = cleaned[2:]
    return ssh_remote_command(host, ["setfattr", "-n", "trusted.glusterfs.mdata", "-v", f"0x{cleaned}", "--", path])


def _ssh_stat_preview(host: str, path: str) -> list[str]:
    return ssh_remote_command(host, ["stat", "-c", "%F", "--", path])


def _ssh_getfattr_preview(host: str, path: str) -> list[str]:
    return ssh_remote_command(host, ["getfattr", "-n", "trusted.gfid", "-e", "hex", "--", path])


def _gluster_split_brain_preview(
    volume: str,
    logical_path: str,
    *,
    winner_host: str = "",
    brick_path: str = "",
    native_policy: str = DEFAULT_SPLIT_BRAIN_NATIVE_POLICY,
) -> list[str]:
    policy = _normalize_split_brain_native_policy(native_policy)
    if not volume:
        return []
    prefix = ["sudo", "-n", "gluster"]
    if policy == "source-brick":
        if winner_host and brick_path:
            return [
                *prefix,
                "volume",
                "heal",
                volume,
                "split-brain",
                "source-brick",
                f"{winner_host}:{brick_path}",
                f"/{logical_path}",
            ]
        return []
    if policy == "latest-mtime":
        return [
            *prefix,
            "volume",
            "heal",
            volume,
            "split-brain",
            "latest-mtime",
            f"/{logical_path}",
        ]
    if policy == "bigger-file":
        return [
            *prefix,
            "volume",
            "heal",
            volume,
            "split-brain",
            "bigger-file",
            f"/{logical_path}",
        ]
    if policy == "auto":
        return [
            *prefix,
            "volume",
            "heal",
            volume,
            "split-brain",
            "latest-mtime",
            f"/{logical_path}",
        ]
    return []


def _brick_root_from_backend(backend_path: str, logical_path: str) -> str:
    """Recover a brick root only when a recorded backend has the exact logical suffix."""
    backend = str(backend_path or "").rstrip("/")
    logical = str(logical_path or "").strip("/")
    suffix = f"/{logical}" if logical else ""
    if not backend or not suffix or not backend.endswith(suffix):
        return ""
    return backend[: -len(suffix)] or "/"


def _backup_base(action: dict[str, object], backup_root: str | None) -> str:
    stage_local_path = str(action.get("stage_local_path") or "")
    logical_path = str(action.get("logical_path") or "")
    if not backup_root:
        if not stage_local_path and logical_path:
            stage_local_path = default_stage_local_path(logical_path)
        marker = "/gluster-repair-stage/"
        if marker in stage_local_path:
            suffix = stage_local_path.split(marker, 1)[1]
        else:
            suffix = stage_local_path.lstrip("/")
        return f"/var/tmp/gluster-repair/backups/{suffix}.backup"
    marker = "/gluster-repair-stage/"
    if marker in stage_local_path:
        suffix = stage_local_path.split(marker, 1)[1]
    elif not stage_local_path and logical_path:
        stage_local_path = default_stage_local_path(logical_path)
        if marker in stage_local_path:
            suffix = stage_local_path.split(marker, 1)[1]
        else:
            suffix = stage_local_path.lstrip("/")
    else:
        suffix = stage_local_path.lstrip("/")
    return f"{backup_root.rstrip('/')}/gluster-repair-backups/{suffix}.backup"


def _backup_target(
    action: dict[str, object],
    backup_root: str | None,
    host: str,
    kind: str,
    basename: str,
) -> str:
    return f"{_backup_base(action, backup_root)}/{host}/{kind}/{host}__{kind}__{basename}"


def _basename(path: str) -> str:
    return Path(path).name or "root"


def _stale_index_ghost_paths(path: str) -> list[str]:
    value = str(path or "").strip()
    if not value or "/.glusterfs/indices/" in value:
        return []
    marker = "/.glusterfs/"
    marker_index = value.find(marker)
    if marker_index < 0:
        return []
    basename = _basename(value)
    if not basename or basename in {".", ".."}:
        return []
    roots = [value[: marker_index + len(marker)]]
    if "/brick/.glusterfs/" not in value:
        brick_root = f"{value[:marker_index]}/brick/.glusterfs/"
        if brick_root not in roots:
            roots.append(brick_root)
    ghost_paths: list[str] = []
    for root in roots:
        ghost_paths.append(f"{root}indices/xattrop/{basename}")
        ghost_paths.append(f"{root}indices/dirty/{basename}")
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in ghost_paths:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def _append_stale_index_ghost_steps(
    result: ApplyActionResult,
    *,
    action_id: str,
    host: str,
    path: str,
    index: int,
    notes: list[str] | None = None,
) -> int:
    for ghost_path in _stale_index_ghost_paths(path):
        result.steps.append(
            ApplyStep(
                step_id=_step_id(action_id, index, "rm-stale-index-ghost"),
                step_type="remove_stale_index_ghost",
                host=host,
                target_path=ghost_path,
                tolerate_missing=True,
                command_preview=_ssh_rm_preview(host, ghost_path),
                notes=notes
                or [
                    "delete matching stale xattrop/dirty ghost for the same GFID",
                    "harmless to prune by default; Gluster can recreate it if needed",
                ],
            )
        )
        index += 1
    return index


def _deepest_first(paths: list[str]) -> list[str]:
    return sorted(
        paths,
        key=lambda path: (
            -str(path).strip("/").count("/"),
            -len(str(path).strip("/")),
            str(path),
        ),
    )


def _add_revert_dir(result: ApplyActionResult, path: str) -> None:
    parent = dirname(path.rstrip("/")) if path else ""
    if parent and parent not in result.revert_dirs_to_create:
        result.revert_dirs_to_create.append(parent)


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _normalize_decision(action: dict[str, object], decisions: dict[str, dict[str, object]] | None) -> dict[str, object]:
    if not decisions:
        return {}
    choice = decisions.get(str(action.get("logical_path") or ""))
    return dict(choice) if isinstance(choice, dict) else {}


def _directory_tie_decision_choice(decision: dict[str, object]) -> str:
    choice = str(decision.get("directory_choice") or decision.get("choice") or "").strip()
    return choice.replace("-", "_").replace(" ", "_").lower()


def _directory_tie_effective_choice(decision: dict[str, object]) -> str:
    choice = _directory_tie_decision_choice(decision)
    if choice != "auto":
        return choice
    recommended = str(decision.get("recommended_choice") or "").strip()
    recommended = recommended.replace("-", "_").replace(" ", "_").lower()
    return recommended or "defer"


def _file_quarantine_decision_choice(decision: dict[str, object]) -> str:
    choice = str(decision.get("file_choice") or decision.get("choice") or "").strip()
    return choice.replace("-", "_").replace(" ", "_").lower()


def _directory_child_signatures(action: dict[str, object]) -> list[tuple[str, ...]]:
    child_names_by_host = action.get("directory_child_names_by_host") or {}
    if not isinstance(child_names_by_host, dict):
        return []
    signatures: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for names in child_names_by_host.values():
        if not isinstance(names, list):
            continue
        signature = tuple(sorted(str(name) for name in names if str(name).strip()))
        if not signature or signature in seen:
            continue
        seen.add(signature)
        signatures.append(signature)
    return signatures


def _directory_conflict_mismatch_hosts(action: dict[str, object]) -> list[str]:
    canonical_gfid = str(action.get("directory_canonical_gfid") or "").strip()
    if not canonical_gfid:
        return []
    mismatch_hosts: list[str] = []
    for copy in action.get("directory_copies") or []:
        if not isinstance(copy, dict):
            continue
        host = str(copy.get("host") or "").strip()
        if not host:
            continue
        identity = str(copy.get("identity") or copy.get("backend_trusted_gfid") or copy.get("gfid") or "").strip()
        if identity and identity != canonical_gfid:
            mismatch_hosts.append(host)
    return sorted(dict.fromkeys(mismatch_hosts))


def _directory_merge_is_safe(action: dict[str, object]) -> bool:
    if not action.get("directory_canonical_gfid") or not action.get("directory_canonical_backend"):
        return False
    if action.get("missing_hosts"):
        return False
    signatures = _directory_child_signatures(action)
    if len(signatures) != 1:
        return False
    if not signatures[0]:
        return False
    return bool(_directory_conflict_mismatch_hosts(action))


def _copy_identity(copy: dict[str, object]) -> str:
    return str(copy.get("identity") or copy.get("backend_trusted_gfid") or copy.get("file_gfid") or "")


def _file_metadata_mismatch_hosts_from_action(action: dict[str, object]) -> list[str]:
    canonical_gfid = str(action.get("winner_file_gfid") or "")
    if not canonical_gfid:
        return []
    mismatch_hosts: list[str] = []
    for copy in action.get("file_copies") or []:
        host = str(copy.get("host") or "")
        if not host:
            continue
        if _copy_identity(copy) != canonical_gfid:
            mismatch_hosts.append(host)
            continue
        if str(copy.get("backend_trusted_gfid") or "") != canonical_gfid:
            mismatch_hosts.append(host)
    return mismatch_hosts


def _resolve_file_decision_copy(
    action: dict[str, object],
    decision: dict[str, object],
) -> tuple[dict[str, object] | None, str]:
    copies = list(action.get("file_copies") or [])
    if not copies:
        return (None, "no file copy candidates recorded in plan")
    keep_gfid = str(decision.get("keep_gfid") or "")
    if keep_gfid:
        matches = [copy for copy in copies if _copy_identity(copy) == keep_gfid]
        if not matches:
            return (None, f"decision keep_gfid={keep_gfid} does not match any file cohort")
        if len(matches) > 1:
            matches = sorted(
                matches,
                key=lambda item: (
                    int(item.get("mtime") or -1),
                    int(item.get("size") or -1),
                    str(item.get("host") or ""),
                ),
                reverse=True,
            )
        return (matches[0], "")
    keep_host = str(decision.get("keep_host") or "")
    if keep_host:
        matches = [copy for copy in copies if str(copy.get("host") or "") == keep_host]
        if not matches:
            return (None, f"decision keep_host={keep_host} does not match any file copy")
        matches = sorted(
            matches,
            key=lambda item: (
                int(item.get("mtime") or -1),
                int(item.get("size") or -1),
                _copy_identity(item),
            ),
            reverse=True,
        )
        return (matches[0], "")
    return (None, "decision file did not provide keep_gfid or keep_host")


def _normalize_split_brain_policy(policy: str) -> str:
    normalized = str(policy or "").strip().lower()
    if normalized == "replace":
        normalized = "auto"
    if normalized == "quarantine":
        return "quarantine"
    if normalized in {"off", "review", "skip"}:
        return "review"
    if normalized in {"auto", "majority", "mtime", "ctime", "size"}:
        return normalized
    return "review"


def _normalize_split_brain_native_policy(policy: str) -> str:
    normalized = str(policy or "").strip().lower().replace("_", "-")
    if normalized in {"", "default"}:
        return DEFAULT_SPLIT_BRAIN_NATIVE_POLICY
    if normalized in {"auto", "source-brick", "latest-mtime", "bigger-file"}:
        return normalized
    return DEFAULT_SPLIT_BRAIN_NATIVE_POLICY


_SPLIT_BRAIN_FILE_TEMP_BACKUP_BUDGET_BYTES = 8 * 1024 * 1024


def _split_brain_file_recovery_mode(
    action: dict[str, object],
    policy: str,
    *,
    backup_mode: str,
) -> tuple[str, str]:
    normalized = _normalize_split_brain_policy(policy)
    if normalized == "quarantine":
        return (
            "quarantine_loser",
            "configured batch policy requests quarantine instead of temp staging",
        )
    if normalized == "review":
        return ("review", f"configured batch policy: {normalized}")

    if backup_mode == "none":
        return (
            "quarantine_loser",
            "backup mode is none; prefer reversible quarantine over temp staging",
        )

    estimated_stage_bytes = _int_value(action.get("estimated_stage_bytes"))
    if not estimated_stage_bytes:
        estimated_stage_bytes = _int_value(action.get("winner_size"))
    stale_backend_count = sum(
        len(paths)
        for paths in (action.get("stale_backends_by_host") or {}).values()
        if isinstance(paths, list)
    )

    if normalized in {"auto", "majority"}:
        if estimated_stage_bytes and estimated_stage_bytes > _SPLIT_BRAIN_FILE_TEMP_BACKUP_BUDGET_BYTES:
            return (
                "quarantine_loser",
                f"estimated stage size {estimated_stage_bytes} bytes exceeds the temp-backup budget",
            )
        if stale_backend_count > 1:
            return (
                "quarantine_loser",
                f"{stale_backend_count} stale backend copies are recorded; prefer quarantine over temp backup",
            )
        return ("temp_backup", f"configured batch policy: {normalized}; stage the winner locally before cleanup")

    if normalized in {"mtime", "size"}:
        if estimated_stage_bytes and estimated_stage_bytes > _SPLIT_BRAIN_FILE_TEMP_BACKUP_BUDGET_BYTES:
            return (
                "quarantine_loser",
                f"estimated stage size {estimated_stage_bytes} bytes exceeds the temp-backup budget",
            )
        return ("temp_backup", f"configured batch policy: {normalized}; preserve the winner in temp before cleanup")

    if normalized == "ctime":
        return (
            "review",
            "ctime policy is not available because ctime is not recorded in the plan",
        )

    return ("review", f"configured batch policy: {normalized}")


def _representative_copy_for_identity(
    action: dict[str, object],
    identity: str,
) -> dict[str, object] | None:
    copies = [
        copy
        for copy in action.get("file_copies") or []
        if str(copy.get("identity") or "") == identity
    ]
    if not copies:
        return None
    copies.sort(
        key=lambda item: (
            int(item.get("mtime") or -1),
            int(item.get("size") or -1),
            str(item.get("host") or ""),
        ),
        reverse=True,
    )
    return copies[0]


def _select_copy_by_metric(
    copies: list[dict[str, object]],
    *,
    metric: str,
) -> tuple[dict[str, object] | None, str]:
    usable = [copy for copy in copies if isinstance(copy.get(metric), int)]
    if not usable:
        return (None, f"no usable {metric} values recorded in file copies")
    top_value = max(int(copy.get(metric) or -1) for copy in usable)
    winners = [copy for copy in usable if int(copy.get(metric) or -1) == top_value]
    if len(winners) != 1:
        return (None, f"{metric} winner is tied")
    return (winners[0], f"{metric} winner={top_value}")


def _select_split_brain_file_copy(
    action: dict[str, object],
    policy: str,
) -> tuple[dict[str, object] | None, str]:
    copies = list(action.get("file_copies") or [])
    cohorts = list(action.get("file_cohorts") or [])
    if not copies:
        return (None, "no file copy candidates recorded in plan")

    normalized = _normalize_split_brain_policy(policy)
    if normalized == "review":
        return (None, f"configured batch policy: {normalized}")
    if normalized == "ctime":
        return (None, "ctime policy is not available because ctime is not recorded in the plan")

    cohorts = list(action.get("file_cohorts") or [])
    if normalized in {"auto", "majority"} and cohorts:
        majority_identity = str(cohorts[0].get("identity") or "")
        majority_hosts = list(cohorts[0].get("hosts") or [])
        runner_up = len(cohorts[1].get("hosts") or []) if len(cohorts) > 1 else 0
        if majority_identity and len(majority_hosts) > runner_up:
            selected = _representative_copy_for_identity(action, majority_identity)
            if selected:
                return (
                    selected,
                    f"majority winner identity={majority_identity} hosts={len(majority_hosts)}",
                )
        if normalized == "majority":
            return (None, "no strict majority winner")

    if normalized in {"auto", "mtime"}:
        candidates: list[tuple[dict[str, object], int]] = []
        source = cohorts if cohorts else copies
        for item in source:
            if cohorts:
                identity = str(item.get("identity") or "")
                representative = _representative_copy_for_identity(action, identity)
                if not representative:
                    continue
                metric_value = representative.get("mtime")
            else:
                representative = item
                metric_value = representative.get("mtime")
            if not isinstance(metric_value, int):
                continue
            candidates.append((representative, int(metric_value)))
        if candidates:
            top_value = max(metric for _, metric in candidates)
            winners = [item for item in candidates if item[1] == top_value]
            if len(winners) == 1:
                return (winners[0][0], f"mtime winner={top_value}")
            if normalized == "mtime":
                return (None, "mtime winner is tied")
        selected, reason = _select_copy_by_metric(copies, metric="mtime")
        if selected:
            return (selected, reason)
        if normalized == "mtime":
            return (None, reason)

    if normalized == "size":
        selected, reason = _select_copy_by_metric(copies, metric="size")
        if selected:
            return (selected, reason)
        return (None, reason)

    if normalized == "auto":
        return (None, "no strict majority winner and no unique mtime winner")
    return (None, f"configured batch policy: {normalized}")


def _action_with_decision_winner(
    action: dict[str, object],
    selected: dict[str, object],
) -> dict[str, object]:
    updated = dict(action)
    copies = list(action.get("file_copies") or [])
    winner_identity = _copy_identity(selected)
    updated["winner_host"] = str(selected.get("host") or "")
    updated["winner_backend"] = str(selected.get("backend") or "")
    updated["winner_mtime"] = selected.get("mtime")
    updated["winner_size"] = selected.get("size")
    updated["winner_file_gfid"] = str(
        selected.get("backend_trusted_gfid") or selected.get("file_gfid") or winner_identity
    )

    healthy_hosts: list[str] = []
    conflict_hosts: list[str] = []
    stale_hosts: list[str] = []
    stale_backends: list[str] = []
    stale_backends_by_host: dict[str, list[str]] = {}
    stale_file_gfid_paths: list[str] = []
    stale_file_gfid_paths_by_host: dict[str, list[str]] = {}

    for copy in copies:
        host = str(copy.get("host") or "")
        identity = _copy_identity(copy)
        backend = str(copy.get("backend") or "")
        if identity and winner_identity and identity == winner_identity:
            if host and host not in healthy_hosts:
                healthy_hosts.append(host)
            continue
        if host and host not in conflict_hosts:
            conflict_hosts.append(host)
        if host and host not in stale_hosts:
            stale_hosts.append(host)
        if backend and backend not in stale_backends:
            stale_backends.append(backend)
            stale_backends_by_host.setdefault(host, []).append(backend)
        file_gfid_path = str(copy.get("file_gfid_path") or "")
        if file_gfid_path and file_gfid_path not in stale_file_gfid_paths:
            stale_file_gfid_paths.append(file_gfid_path)
            stale_file_gfid_paths_by_host.setdefault(host, []).append(file_gfid_path)

    all_hosts = set(healthy_hosts) | set(conflict_hosts) | set(action.get("missing_hosts") or [])
    missing_hosts = sorted(host for host in all_hosts if host and host not in healthy_hosts and host not in conflict_hosts)

    updated["healthy_hosts"] = sorted(healthy_hosts)
    updated["conflict_hosts"] = sorted(conflict_hosts)
    updated["stale_hosts"] = sorted(stale_hosts)
    updated["missing_hosts"] = missing_hosts
    updated["stale_backends"] = stale_backends
    updated["stale_backends_by_host"] = stale_backends_by_host
    updated["stale_file_gfid_paths"] = stale_file_gfid_paths
    updated["stale_file_gfid_paths_by_host"] = stale_file_gfid_paths_by_host
    return updated
