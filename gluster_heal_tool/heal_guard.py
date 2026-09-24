# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Heal-state guard helpers."""
from __future__ import annotations

import hashlib

from .heal_parser import parse_heal_info_text
from .volume import get_heal_info_text


DEFAULT_HEAL_INFO_REPEAT_LIMIT = 5
DEFAULT_HEAL_INFO_REFRESH_TIMEOUT_SECONDS = 60.0
DEFAULT_HEAL_INFO_CHURN_LIMIT = 10


def build_heal_info_snapshot(volume: str) -> dict[str, object]:
    try:
        heal_text = get_heal_info_text(volume)
    except Exception as exc:  # pragma: no cover - best-effort live check
        return {
            "available": False,
            "volume": volume,
            "entry_count": 0,
            "unique_count": 0,
            "sample_paths": [],
            "unique_signature": "",
            "error": str(exc),
        }
    entries = parse_heal_info_text(heal_text)
    unique_paths: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.raw in seen:
            continue
        seen.add(entry.raw)
        unique_paths.append(entry.raw)
    signature_source = "\n".join(sorted(seen))
    unique_signature = hashlib.sha256(signature_source.encode("utf-8")).hexdigest() if seen else ""
    return {
        "available": True,
        "volume": volume,
        "entry_count": len(entries),
        "unique_count": len(unique_paths),
        "sample_paths": unique_paths[:5],
        "unique_signature": unique_signature,
        "error": "",
    }


def assess_heal_info_repeat(
    previous_status: dict[str, object],
    current_snapshot: dict[str, object],
    *,
    repeat_limit: int = DEFAULT_HEAL_INFO_REPEAT_LIMIT,
    churn_limit: int = DEFAULT_HEAL_INFO_CHURN_LIMIT,
) -> dict[str, object]:
    normalized_limit = max(2, int(repeat_limit))
    normalized_churn_limit = max(2, int(churn_limit))
    available = bool(current_snapshot.get("available"))
    current_signature = str(current_snapshot.get("unique_signature") or "")
    current_unique_count = int(current_snapshot.get("unique_count", 0) or 0)
    previous_guard = previous_status.get("final_check_guard") or {}
    previous_check = previous_status.get("final_check") or {}
    previous_signature = str(
        previous_guard.get("signature")
        or previous_check.get("unique_signature")
        or ""
    )
    previous_repeat_count = int(previous_guard.get("repeat_count", 0) or 0)
    previous_churn_count = int(previous_guard.get("churn_count", 0) or 0)

    if not available or current_unique_count <= 0:
        return {
            "available": available,
            "signature": current_signature,
            "previous_signature": previous_signature,
            "repeat_count": 0,
            "churn_count": 0,
            "repeat_limit": normalized_limit,
            "churn_limit": normalized_churn_limit,
            "changed": current_signature != previous_signature,
            "hit_limit": False,
            "churn_hit_limit": False,
            "warning": "",
        }

    if not previous_signature:
        repeat_count = 1
        churn_count = 1
        changed = True
    elif current_signature == previous_signature:
        repeat_count = previous_repeat_count + 1 if previous_repeat_count >= 1 else 2
        churn_count = 1
        changed = False
    else:
        repeat_count = 1
        churn_count = previous_churn_count + 1 if previous_churn_count >= 1 else 2
        changed = True

    hit_limit = repeat_count >= normalized_limit
    churn_hit_limit = churn_count >= normalized_churn_limit
    warning = ""
    if hit_limit:
        warning = (
            "post-execute heal info shape has not changed for "
            f"{repeat_count} consecutive checks; probable repair gap "
            "after heal and mount verification"
        )
    elif churn_hit_limit:
        warning = (
            "post-execute heal info shape has changed for "
            f"{churn_count} consecutive checks; probable repair churn "
            "after repeated rescan and verify attempts"
        )
    elif repeat_count > 1:
        warning = (
            "post-execute heal info shape is unchanged for "
            f"{repeat_count} consecutive checks"
        )

    return {
        "available": True,
        "signature": current_signature,
        "previous_signature": previous_signature,
        "repeat_count": repeat_count,
        "churn_count": churn_count,
        "repeat_limit": normalized_limit,
        "churn_limit": normalized_churn_limit,
        "changed": changed,
        "hit_limit": hit_limit,
        "churn_hit_limit": churn_hit_limit,
        "warning": warning,
    }
