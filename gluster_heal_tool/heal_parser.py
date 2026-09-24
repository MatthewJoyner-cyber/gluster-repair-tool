# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Gluster heal info parser helpers."""
from __future__ import annotations

from pathlib import Path

from .models import HealEntry


def parse_heal_info_text(text: str) -> list[HealEntry]:
    entries: list[HealEntry] = []
    current_host = ""
    current_brick = ""
    per_host_index = 0

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("Brick "):
            payload = line[len("Brick ") :]
            host, _, brick = payload.partition(":")
            current_host = host.strip()
            current_brick = brick.strip()
            per_host_index = 0
            continue
        if line.startswith("Status:") or line.startswith("Number of entries:"):
            continue
        entry = line.strip()
        if not entry or not current_host:
            continue
        split_brain = entry.endswith(" - Is in split-brain")
        if split_brain:
            entry = entry.removesuffix(" - Is in split-brain").rstrip()
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
    return entries


def parse_heal_info_file(path: str | Path) -> list[HealEntry]:
    return parse_heal_info_text(Path(path).read_text())
