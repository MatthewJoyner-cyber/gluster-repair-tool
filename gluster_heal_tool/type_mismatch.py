# SPDX-License-Identifier: GPL-2.0-only
"""Type-mismatch preservation policy helpers."""
from __future__ import annotations

from typing import Iterable, Mapping


def type_mismatch_quarantine_recommendation(
    file_copies: Iterable[Mapping[str, object]],
    directory_copies: Iterable[Mapping[str, object]],
    *,
    expected_hosts: Iterable[str] = (),
) -> tuple[str, str, str, str]:
    """Return choice, reason, summary, and the older side when it is proven."""

    def collect(copies: Iterable[Mapping[str, object]]) -> tuple[list[int], set[str], list[str]]:
        mtimes: list[int] = []
        hosts: set[str] = set()
        incomplete: list[str] = []
        for copy in copies:
            host = str(copy.get("host") or "").strip()
            if host:
                hosts.add(host)
            mtime = copy.get("mtime")
            if isinstance(mtime, int):
                mtimes.append(mtime)
            else:
                incomplete.append(host or "unknown-host")
        return mtimes, hosts, incomplete

    file_mtimes, file_hosts, file_incomplete = collect(file_copies)
    directory_mtimes, directory_hosts, directory_incomplete = collect(directory_copies)
    missing_hosts = sorted(
        {str(host).strip() for host in expected_hosts if str(host).strip()}
        - file_hosts
        - directory_hosts
    )
    if not file_mtimes:
        file_incomplete.append("no-file-copy")
    if not directory_mtimes:
        directory_incomplete.append("no-directory-copy")
    if file_incomplete or directory_incomplete or missing_hosts:
        gaps: list[str] = []
        if file_incomplete:
            gaps.append("file side incomplete: " + ", ".join(sorted(set(file_incomplete))))
        if directory_incomplete:
            gaps.append("directory side incomplete: " + ", ".join(sorted(set(directory_incomplete))))
        if missing_hosts:
            gaps.append("hosts without a visible branch: " + ", ".join(missing_hosts))
        return (
            "quarantine_both",
            "Backend mtime evidence is incomplete (" + "; ".join(gaps) + "); quarantine both branches first.",
            "incomplete",
            "",
        )

    file_min, file_max = min(file_mtimes), max(file_mtimes)
    directory_min, directory_max = min(directory_mtimes), max(directory_mtimes)
    summary = f"file={file_min}-{file_max}; directory={directory_min}-{directory_max}"
    if file_max < directory_min:
        return (
            "quarantine_loser",
            f"File-side backend mtime range {file_min}-{file_max} is older than directory-side range {directory_min}-{directory_max}; quarantine the older file branch.",
            summary + "; loser=file",
            "file",
        )
    if directory_max < file_min:
        return (
            "quarantine_loser",
            f"Directory-side backend mtime range {directory_min}-{directory_max} is older than file-side range {file_min}-{file_max}; quarantine the older directory branch.",
            summary + "; loser=directory",
            "directory",
        )
    return (
        "quarantine_both",
        f"Backend mtime ranges overlap or tie ({summary}); quarantine both branches first.",
        summary + "; overlap",
        "",
    )
