# SPDX-License-Identifier: GPL-2.0-only
"""Repair matrix helpers."""
from __future__ import annotations

import json

REPAIR_MATRIX_ROWS = [
    {
        "key": "file_restore",
        "situation": "File present on quorum or majority, missing on fewer replicas",
        "default_repair_path": "restore_missing_replica",
        "status": "executable for canaries",
        "decision_class": "safe_default",
        "notes": "Recreate the missing replica(s) after staging the winner copy. When the missing copy also carries stale child/GFID residue, the child-gap variant can use restore_missing_child_replica so the restore path stays explicit.",
    },
    {
        "key": "file_delete_below_quorum",
        "situation": "File present on fewer than quorum replicas",
        "default_repair_path": "delete_below_quorum_file",
        "status": "review / synthetic-covered",
        "decision_class": "risky_default",
        "notes": "Treat as a likely stale survivor or orphan; do not recreate by default. Exact-half replica-2 and replica-4 shapes are excluded from this delete path because they have no strict-majority repair authority. Brick-side salvage remains explicit and replica-4+ only until separately proven.",
    },
    {
        "key": "file_presence_tie",
        "situation": "File present on exactly half of an even-width replica set",
        "default_repair_path": "quarantine_both, then explicit restore-or-delete decision",
        "status": "review with executable preservation choice",
        "decision_class": "operator_only",
        "notes": "Client quorum is not strict-majority repair authority. Even when all visible copies share one GFID, do not infer restore or delete intent; preserve every visible copy before an explicit decision.",
    },
    {
        "key": "file_orphaned_symlink_cleanup",
        "situation": "Orphaned symlink entry present on fewer than quorum replicas",
        "default_repair_path": "delete_orphaned_symlink_residue",
        "status": "delete by policy / review unless configured",
        "decision_class": "risky_default",
        "notes": "Treat the symlink inode as file-family; if the symlink itself is the surviving residue, back it up and delete the backend and GFID remnants. External targets stay outside Gluster repair.",
    },
    {
        "key": "file_split_brain_majority",
        "situation": "File split-brain, clear 3 vs 1 or similar majority winner",
        "default_repair_path": "replace_entry_split_brain_file",
        "status": "executable for canaries",
        "decision_class": "risky_default",
        "notes": "Try the official Gluster split-brain resolver first, then stage the winner, remove the loser backend and file-GFID remnants, and restore once through the mount if needed. Default split-brain policy is auto = majority then mtime; the native Gluster resolver defaults to latest-mtime, while source-brick is the explicit choice when the keeper brick is already known; bigger-file is the other explicit native option; size stays explicit only in the internal fallback path.",
    },
    {
        "key": "file_split_brain_tie",
        "situation": "File split-brain, tied entry/GFID or same-GFID content cohorts",
        "default_repair_path": "quarantine_both, then decision-build / --decision-file",
        "status": "review unless resolved",
        "decision_class": "operator_only",
        "notes": "Recommend quarantine_both first when no winner is proven. Then use checksum and an explicit decision file/source selection; quarantine_loser is appropriate only after a canonical side is recorded.",
    },
    {
        "key": "directory_restore",
        "situation": "Directory missing on one brick, intended to exist",
        "default_repair_path": "recreate_missing_directory_backend",
        "status": "executable with brick-side recreate + GFID attach",
        "decision_class": "safe_default",
        "notes": "Brick-side recreate plus GFID attach, not mount mkdir. When the missing backend entry is part of a child-gap repair story, the explicit child-gap variant can use recreate_missing_directory_backend_child_gap so the repair path stays visible in canary output.",
    },
    {
        "key": "directory_child_gap",
        "situation": "Directory exists, but immediate child sets differ or a child is missing on one or more bricks",
        "default_repair_path": "reconcile_directory_children",
        "status": "review unless child deps are fully executable",
        "decision_class": "chain_follow",
        "notes": "Parent presence or client quorum does not prove the missing child's identity or intent. Promote only when the structural child dependency closure is executable; otherwise collect immediate --gfid-child <parent-gfid>/<child> evidence with mount probing off, then rebuild the plan.",
    },
    {
        "key": "directory_child_reference",
        "situation": "Immediate or nested GFID-child reference points into a directory chain",
        "default_repair_path": "recover_missing_child_or_confirm_delete / follow live chain / cleanup proven nested residue",
        "status": "decision or chain-follow before cleanup",
        "decision_class": "chain_follow",
        "notes": "For an immediate all-missing child, recover from an authoritative backup or confirm deletion; never delete the parent GFID handle or recreate the child without a source. Follow nested live chains first, and clean only a separately proven stale nested tail.",
    },
    {
        "key": "directory_delete_below_quorum",
        "situation": "Directory present on fewer than quorum replicas",
        "default_repair_path": "delete_below_quorum_subtree",
        "status": "review / synthetic-covered",
        "decision_class": "risky_default",
        "notes": "Treat as a stale subtree and remove deepest-first. Exact-half even-width shapes are excluded because they have no strict-majority delete authority. The directory stale-survivor policy defaults to auto/delete for the remaining below-quorum shapes unless the operator overrides it to review.",
    },
    {
        "key": "directory_presence_tie",
        "situation": "Directory present on exactly half of an even-width replica set",
        "default_repair_path": "quarantine_both, then explicit restore-or-delete decision",
        "status": "review with executable preservation choice",
        "decision_class": "operator_only",
        "notes": "Client quorum is not strict-majority repair authority. Even when every visible tree shares one GFID, do not infer restore or subtree-delete intent; preserve every visible tree before an explicit decision.",
    },
    {
        "key": "directory_gfid_merge",
        "situation": "Directory entry split-brain / name-GFID conflict, but the bounded trees collapse cleanly",
        "default_repair_path": "repair_directory_metadata",
        "status": "executable when the bounded trees collapse cleanly",
        "decision_class": "safe_default",
        "notes": "Run directory-tie-build first. When the child sets match, merge can promote into repair_directory_metadata / attach_directory_gfid without recreating the directory tree. The same safe path also covers clear majority-backed directory GFID cases; keep the pure tie branch separate.",
    },
    {
        "key": "directory_split_brain",
        "situation": "Directory entry split-brain / name-GFID conflict, pure tie or unresolved ambiguity",
        "default_repair_path": "quarantine_both, then directory-tie-build",
        "status": "review-only",
        "decision_class": "operator_only",
        "notes": "Recommend quarantine_both first when no canonical tree is proven; use quarantine_loser when a canonical side is recorded. Then run directory-tie-build before any merge, prune, or source decision. Mount-side EIO is evidence for this family even when Gluster's split-brain count is zero.",
    },
    {
        "key": "directory_metadata_repair",
        "situation": "Directory exists everywhere, but one brick is missing/drifting canonical trusted.gfid or trusted.glusterfs.mdata",
        "default_repair_path": "repair_directory_metadata",
        "status": "executable when the directory is otherwise consistent",
        "decision_class": "safe_default",
        "notes": "Attach the canonical directory GFID xattr when identity drifts, or align trusted.glusterfs.mdata when there is a clear majority; do not recreate the directory tree.",
    },
    {
        "key": "directory_metadata_only",
        "situation": "Directory metadata only",
        "default_repair_path": "refresh_directory_evidence or choose_directory_mdata_source",
        "status": "review-only",
        "decision_class": "operator_only",
        "notes": "Fallback only after structure checks are exhausted. Recommend focused child/GFID evidence refresh; when trusted.glusterfs.mdata has no majority, choose an explicit source value. Do not leave the case at keep-review with no next edge.",
    },
    {
        "key": "entry_heal_smoke",
        "situation": "Granular entry-heal / entry self-heal smoke test",
        "default_repair_path": "native-heal/rescan verification",
        "status": "Gluster-handled smoke test",
        "decision_class": "operator_only",
        "notes": "Run native heal/rescan and verify the row clears; keep it as a plumbing smoke test, not a repair-value proof. It is terminal-safe diagnostics, not a dead-end repair answer.",
    },
    {
        "key": "directory_ctime_smoke",
        "situation": "Directory ctime / trusted.glusterfs.mdata smoke test",
        "default_repair_path": "refresh_directory_evidence or choose_directory_mdata_source",
        "status": "review-only / further investigation",
        "decision_class": "operator_only",
        "notes": "Run focused directory metadata evidence and choose an explicit mdata source if the values disagree. If trusted.gfid and child sets agree and two replicas agree on mdata, the directory_metadata_repair row can align the minority brick to the majority.",
    },
    {
        "key": "file_metadata_repair",
        "situation": "File exists everywhere, but one brick is missing the canonical trusted.gfid xattr",
        "default_repair_path": "repair_file_metadata",
        "status": "executable when the file is otherwise consistent",
        "decision_class": "safe_default",
        "notes": "Attach the canonical file GFID to the mismatched bricks; do not recreate the file.",
    },
    {
        "key": "posix_metadata_repair",
        "situation": "POSIX mode/uid/gid/ACL metadata differs but one tuple is a strict majority",
        "default_repair_path": "repair_posix_metadata",
        "status": "executable when the object is otherwise consistent",
        "decision_class": "safe_default",
        "notes": "Align the minority replicas to the strict-majority POSIX tuple; do not recreate the file or directory.",
    },
    {
        "key": "posix_metadata_no_majority",
        "situation": "POSIX mode/uid/gid/ACL metadata differs without a strict majority",
        "default_repair_path": "review_posix_metadata_no_majority",
        "status": "review-only",
        "decision_class": "operator_only",
        "notes": "No strict majority exists, so the operator must choose a source host/value explicitly before any repair can proceed.",
    },
    {
        "key": "file_metadata_only",
        "situation": "File metadata only",
        "default_repair_path": "enable file-metadata repair policy",
        "status": "review-only",
        "decision_class": "policy_gated",
        "notes": "Fallback only after file-family, symlink-chain, split-brain, and stale-survivor checks are exhausted. Recommend rerunning with --policy-file-metadata repair after canonical GFID evidence is confirmed.",
    },
    {
        "key": "file_dead_ref_cleanup",
        "situation": "File has no surviving copy on any replica",
        "default_repair_path": "cleanup_dead_file_refs",
        "status": "executable after no surviving file copy remains",
        "decision_class": "safe_default",
        "notes": "Delete stale backend and GFID residue once no file copy survives anywhere; brick-side cleanup only.",
    },
    {
        "key": "arbiter_only_residue_cleanup",
        "situation": "Replica 3 arbiter placeholder remains after both data copies are absent",
        "default_repair_path": "cleanup_arbiter_residue",
        "status": "live file and directory proof complete",
        "decision_class": "safe_default",
        "notes": "Delete only the arbiter backend placeholder subtree, GFID handle, and index residue. Prefer volume snapshot rollback; recorded best-effort residue backups support a deliberate restore of the old arbiter metadata state but never recover payload or provide authority.",
    },
    {
        "key": "arbiter_data_identity_repair",
        "situation": "Replica 3 arbiter identity agrees with one data file while the other data copy conflicts",
        "default_repair_path": "guided_backup_quarantine_loser_then_explicit_data_source_copy",
        "status": "synthetic-covered; index-heal-only assumption refuted",
        "decision_class": "operator_only",
        "notes": "One matching data copy plus the arbiter is strong authority for that data copy's identity, never for arbiter payload. The guided repair moves the conflicting data backend and GFID handle aside, explicitly copies the matching data-brick payload to the target, then verifies and replans. Do not use ordinary index heal as the source-selection mechanism; preserve-both and skip remain available.",
    },
    {
        "key": "file_handle_ghost_cleanup",
        "situation": "Regular .glusterfs file handle ghost or orphaned hardlink with stale gfid2path target",
        "default_repair_path": "cleanup_dead_gfid",
        "status": "executable after terminal target no longer resolves",
        "decision_class": "safe_default",
        "notes": "If mount-side evidence still sees the file but the gfid2path terminal target does not resolve to a live manifest object, prune the handle and its GFID residue; keep live-reference chains on review_dead_gfid_reference instead.",
    },
    {
        "key": "stale_glusterfs_index_cleanup",
        "situation": "Stale .glusterfs heal index entry under indices/xattrop or indices/dirty",
        "default_repair_path": "cleanup_stale_glusterfs_index",
        "status": "executable delete-by-default cleanup",
        "decision_class": "safe_default",
        "notes": "Delete internal Gluster heal bookkeeping entries directly; do not back them up, and let a later crawl or access recreate them if they still matter.",
    },
    {
        "key": "dead_gfid",
        "situation": "Dead GFID",
        "default_repair_path": "cleanup_dead_gfid",
        "status": "executable after no live references remain",
        "decision_class": "safe_default",
        "notes": "Safe removal only after confirming no live references remain.",
    },
    {
        "key": "dead_gfid_reference",
        "situation": "Dead GFID still resolves to a live path",
        "default_repair_path": "review_dead_gfid_reference",
        "status": "review-only",
        "decision_class": "chain_follow",
        "notes": "Inspect the live reference chain first; if it resolves to no live manifest object, the residue can be cleaned up; if it resolves to a real file or directory branch, follow that branch instead.",
    },
    {
        "key": "post_resolution_ghost_tail",
        "situation": "Post-resolution ghost tail / recurring unresolved-child residue after cleanup",
        "default_repair_path": "cleanup_dead_gfid",
        "status": "executable delete-by-default cleanup with recurrence warning",
        "decision_class": "safe_default",
        "notes": "Delete once the chain proves stale-only; if the same ghost returns after cleanup, inspect .glusterfs references and logs first because recurrence may mean a deeper directory-child or handle-ghost case is still present.",
    },
    {
        "key": "file_symlink",
        "situation": "Symlink entry on the volume",
        "default_repair_path": "restore_missing_replica / delete_below_quorum_file",
        "status": "file-family repair",
        "decision_class": "chain_follow",
        "notes": "Treat the symlink inode itself as file-like; follow GFID-chain terminals when they point into `.glusterfs`, otherwise use the normal file rules even if the target is external or orphaned.",
    },
    {
        "key": "file_type_mismatch",
        "situation": "File type mismatch",
        "default_repair_path": "quarantine_loser when complete mtime ranges prove an older branch; otherwise quarantine_both",
        "status": "review with executable reversible quarantine choices",
        "decision_class": "operator_only",
        "notes": "File vs directory disagreement never auto-selects a source. Recommend quarantine_loser only when every expected host exposes a branch, every branch copy has complete mtime evidence, and the file/directory mtime ranges do not overlap; otherwise recommend quarantine_both. Preserve arbiter-side metadata objects, but never use an arbiter as a payload source. Quarantine preserves evidence; it is not itself a source-selection repair.",
    },
]


FILE_MATRIX_KEYS = (
    "file_restore",
    "file_delete_below_quorum",
    "file_presence_tie",
    "file_orphaned_symlink_cleanup",
    "file_split_brain_majority",
    "file_split_brain_tie",
    "file_metadata_repair",
    "posix_metadata_repair",
    "posix_metadata_no_majority",
    "file_metadata_only",
    "file_dead_ref_cleanup",
    "file_handle_ghost_cleanup",
    "file_symlink",
    "file_type_mismatch",
)

DIRECTORY_MATRIX_KEYS = (
    "directory_restore",
    "directory_child_gap",
    "directory_child_reference",
    "directory_presence_tie",
    "directory_delete_below_quorum",
    "directory_gfid_merge",
    "directory_split_brain",
    "directory_metadata_repair",
    "directory_metadata_only",
)

SMOKE_TEST_MATRIX_KEYS = ("entry_heal_smoke", "directory_ctime_smoke")

RESIDUE_MATRIX_KEYS = (
    "stale_glusterfs_index_cleanup",
    "dead_gfid",
    "dead_gfid_reference",
    "post_resolution_ghost_tail",
)


def _matrix_rows_for(keys: tuple[str, ...]) -> list[dict[str, str]]:
    rows_by_key = {row["key"]: row for row in REPAIR_MATRIX_ROWS}
    return [dict(rows_by_key[key]) for key in keys if key in rows_by_key]


def repair_matrix_family_rows() -> dict[str, list[dict[str, str]]]:
    return {
        "file": _matrix_rows_for(FILE_MATRIX_KEYS),
        "directory": _matrix_rows_for(DIRECTORY_MATRIX_KEYS),
        "smoke": _matrix_rows_for(SMOKE_TEST_MATRIX_KEYS),
        "residue": _matrix_rows_for(RESIDUE_MATRIX_KEYS),
    }


def repair_matrix_rows() -> list[dict[str, str]]:
    return [dict(row) for row in REPAIR_MATRIX_ROWS]


def repair_matrix_row(key: str) -> dict[str, str] | None:
    for row in REPAIR_MATRIX_ROWS:
        if row["key"] == key:
            return dict(row)
    return None


def render_repair_matrix() -> str:
    header = ["Key", "Situation", "Default repair path", "Decision class", "Current status", "Notes"]
    rows = [
        [
            row["key"],
            row["situation"],
            row["default_repair_path"],
            row["decision_class"],
            row["status"],
            row["notes"],
        ]
        for row in REPAIR_MATRIX_ROWS
    ]
    widths = [len(col) for col in header]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]

    def fmt(row: list[str]) -> str:
        return " | ".join(cell.ljust(width) for cell, width in zip(row, widths))

    lines = [
        "# Repair Matrix",
        "",
        fmt(header),
        " | ".join("-" * width for width in widths),
    ]
    lines.extend(fmt(row) for row in rows)
    lines.extend(
        [
            "",
            "Rules of thumb:",
        "- Use `restore` for intended live entries that are under-replicated.",
        "- Decision classes: `safe_default` means low-risk automatic repair/cleanup, `risky_default` means the default is still allowed but should warn the operator, `chain_follow` means follow the dependency/reference chain and rescan before acting, `policy_gated` means an explicit operator policy is needed to promote, and `operator_only` means the branch stays manual.",
        "- For file split-brain, use `auto` as majority then mtime; keep `size` explicit and do not use it as an automatic fallback.",
        "- Treat the heal list as a best-guess snapshot once the crawl settles, and prefer a full heal first when you can because Gluster will usually surface remaining mismatches as the crawl progresses.",
        "- If a file split-brain candidate has not shown up in `heal info` yet, treat that as scan lag or stale bookkeeping, not as proof that the GFID is dead.",
        "- When the official Gluster resolver is in play, use `latest-mtime` as the native default; choose `source-brick` only when the keeper brick is already known; `bigger-file` remains an explicit native option and ambiguous source-brick requests stay review-only.",
        "- Try the official Gluster split-brain resolver first; fall back to the brick-side cleanup/recreate path only if Gluster does not resolve the file.",
        "- Use `delete` for likely stale survivors or orphans.",
        "- Use `cleanup_dead_file_refs` when a file has no surviving copy on any replica; delete the brick-side residue after confirming nothing real remains.",
        "- Use `cleanup_stale_glusterfs_index` for stale `.glusterfs/indices/xattrop` or `.glusterfs/indices/dirty` bookkeeping entries; delete them by default and do not back them up.",
        "- Treat `entry_heal_smoke` as a Gluster-handled smoke test, not a repair-value proof; it is useful for proving the entry-heal path, but Gluster can self-heal it when granular entry heal is active.",
        "- Use `review_directory_children` when a nested GFID-child residue keeps pointing into a directory chain; treat it as directory-family evidence before cleanup.",
        "- For directory entry split-brain / name-GFID conflicts, run `directory-tie-build` first; if the child sets collapse cleanly, `merge` can promote to `repair_directory_metadata`, and if one GFID is a clear majority, promote to `repair_directory_metadata` directly. Keep pure ties review-only and rename/quarantine the losing tree when the branch remains ambiguous.",
        "- Treat symlink entries as file subcategories; use normal file rules once the symlink object itself is confirmed live.",
        "- Orphaned symlink residues can be cleaned when they are below quorum; keep true live symlink entries on the file path.",
        "- Use `review` when the evidence is mixed.",
        "- Use `repair_directory_metadata` when the directory exists everywhere but one brick is missing the canonical `trusted.gfid` xattr, or when `trusted.glusterfs.mdata` has a clear majority and only the minority bricks need alignment.",
        "- Avoid `sync` as a repair label; it is too vague for this repair model.",
        "- If a behavior is uncertain, prove it on `gtest` first, then freeze it in synthetic regression.",
    ]
    )
    return "\n".join(lines)


def render_repair_matrix_json() -> str:
    return json.dumps({"rows": repair_matrix_rows()}, indent=2, sort_keys=True)
