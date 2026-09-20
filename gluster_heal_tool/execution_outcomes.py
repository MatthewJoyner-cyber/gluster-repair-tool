# SPDX-License-Identifier: GPL-2.0-only
"""Narrow command-response contracts; unexpected diagnostics stop execution."""
from __future__ import annotations

import subprocess

from .models import ApplyStep


def command_message(completed: subprocess.CompletedProcess[str]) -> str:
    """Keep both streams: a benign stdout must not hide a failing stderr."""
    return "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip())


def native_resolver_mode(command: list[str]) -> str:
    # Match argv positions, not text inside a volume, brick or file name.
    if command[:5] != ["sudo", "-n", "gluster", "volume", "heal"]:
        return ""
    if len(command) < 9 or command[6] != "split-brain":
        return ""
    mode = command[7]
    if mode in {"latest-mtime", "bigger-file"} and len(command) == 9:
        return mode
    if mode == "source-brick" and len(command) == 10:
        return mode
    return ""


def native_resolver_outcome(command: list[str], returncode: int | None, message: str) -> str:
    """Accept only supported, complete English responses for the requested file.

    Exit zero alone is insufficient: glfsheal can print a GFID-heal failure
    while returning zero. Unrecognized versions/locales require review.
    """
    mode = native_resolver_mode(command)
    if not mode or returncode not in {0, 1}:
        return "unknown"
    path = command[-1]
    if not path.startswith("/") or any(character in path for character in "\r\n"):
        return "unknown"
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        return "unknown"
    gfid_tie = f"No difference in {'size' if mode == 'bigger-file' else 'mtime'} for file {path}"
    gfid_success = f"GFID split-brain resolved for file {path}"
    # glfsheal's GFID response may follow this exact lookup diagnostic. It is
    # not permission to ignore EIO from other commands, paths or output shapes.
    if len(lines) == 2 and returncode == 0 and lines[0] == f"Lookup failed on {path}:Input/output error.":
        if lines[1] not in {gfid_success, gfid_tie}:
            return "unknown"
        lines = lines[1:]
    if len(lines) != 1:
        return "unknown"
    response = lines[0]
    if returncode == 0 and response in {f"Healed {path}.", gfid_success}:
        return "resolved"
    if response == f"Healing {path} failed: File not in split-brain.":
        return "already-resolved"
    if mode in {"latest-mtime", "bigger-file"}:
        field = "mtime" if mode == "latest-mtime" else "size"
        if response in {gfid_tie, f"Healing {path} failed: No difference in {field}."}:
            return "tie"
    return "unknown"


def missing_removal_tolerated(step: ApplyStep, completed: subprocess.CompletedProcess[str]) -> bool:
    """Only explicit ENOENT for an optional removal's exact target can skip.

    A missing backup/restore/quarantine source is stale execution evidence,
    even if an older plan labelled it best-effort. It must stop before removal.
    """
    if not step.tolerate_missing or not step.step_type.startswith("remove_"):
        return False
    if completed.returncode != 1 or (completed.stdout or "").strip() or not step.target_path:
        return False
    if any(character in step.target_path for character in "\r\n"):
        return False
    return (completed.stderr or "").strip() in {
        f"rm: cannot remove '{step.target_path}': No such file or directory",
        f"rm: cannot remove ‘{step.target_path}’: No such file or directory",
    }
