# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Gluster heal info parser helpers."""
from __future__ import annotations

from pathlib import Path
import re

from .models import HealEntry


GFID_ROW = re.compile(r"<gfid:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}>(?:/.*)?")


def parse_heal_info_text(text: str, *, require_connected: bool = False) -> list[HealEntry]:
    """Parse known rows; live completion checks require complete connected sections."""
    entries: list[HealEntry] = []
    current_host = ""
    current_brick = ""
    per_host_index = 0
    status = ""
    reported_count: int | None = None
    brick_count = 0

    def finish_brick() -> None:
        if not require_connected or not current_host:
            return
        if status != "Connected":
            raise RuntimeError(f"heal info connection state is unavailable for {current_host}")
        if reported_count is None or reported_count != per_host_index:
            raise RuntimeError(f"heal info entry count is unavailable or inconsistent for {current_host}")

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("Brick "):
            finish_brick()
            payload = line[len("Brick ") :]
            host, separator, brick = payload.partition(":")
            current_host = host.strip()
            current_brick = brick.strip()
            if not separator or not current_host or not current_brick.startswith("/"):
                if require_connected:
                    raise RuntimeError("heal info has an unsupported brick header")
                current_host = ""
                current_brick = ""
            per_host_index = 0
            status = ""
            reported_count = None
            brick_count += 1
            continue
        if line.startswith("Status:"):
            if require_connected and status:
                raise RuntimeError("heal info repeats a brick status")
            status = line.partition(":")[2].strip()
            if require_connected and (not current_host or status != "Connected"):
                raise RuntimeError("heal info brick status is missing or unqualified")
            continue
        if line.startswith("Number of entries:"):
            if require_connected and reported_count is not None:
                raise RuntimeError("heal info repeats a brick entry count")
            value = line.partition(":")[2].strip()
            if require_connected:
                if not current_host or not value.isdecimal():
                    raise RuntimeError("heal info entry count is missing or unqualified")
                reported_count = int(value)
            continue
        entry = line.strip()
        if not entry:
            continue
        if not current_host:
            if require_connected:
                raise RuntimeError("heal info contains text outside a brick section")
            continue
        split_brain = entry.endswith(" - Is in split-brain")
        if split_brain:
            entry = entry.removesuffix(" - Is in split-brain").rstrip()
        if not entry.startswith("/") and not GFID_ROW.fullmatch(entry):
            if require_connected:
                raise RuntimeError("heal info contains an unqualified row format")
            continue
        per_host_index += 1
        entries.append(
            HealEntry(
                raw=entry,
                source_host=current_host,
                brick=current_brick,
                index_on_host=per_host_index,
                split_brain=split_brain,
            )
        )
    finish_brick()
    if require_connected and not brick_count:
        raise RuntimeError("heal info has no brick sections")
    return entries


def parse_heal_info_file(path: str | Path) -> list[HealEntry]:
    return parse_heal_info_text(Path(path).read_text())
