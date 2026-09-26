# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Features qualified on an exact Gluster release in a disposable live lab.

An installed version identifies a candidate profile; it does not prove that a
command succeeded or that an unfamiliar response has a known meaning.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable


QUALIFIED_FEATURES: dict[str, frozenset[str]] = {
    # Three Ubuntu guests exercised pending index healing on Gluster 11.1.
    # Full namespace heal and native split-brain writes lack live qualification.
    "11.1": frozenset({"pending_index_heal"}),
}


def feature_qualified(version: str, feature: str) -> bool:
    return feature in QUALIFIED_FEATURES.get(version.strip(), frozenset())


def require_qualified_feature(feature: str, get_version: Callable[[], str], action: str) -> None:
    """Check a live version immediately before dispatching a Gluster write."""
    try:
        version = get_version()
    except (RuntimeError, OSError) as exc:
        raise RuntimeError(f"{action} needs a qualified Gluster version: {exc}") from exc
    if not feature_qualified(version, feature):
        raise RuntimeError(f"{action} is unqualified on Gluster {version}")


def require_execution_features(results: Iterable[object], get_version: Callable[[], str]) -> None:
    """Refuse an unqualified native resolver before any planned action starts."""
    native = any(
        step.step_type == "resolve_split_brain_gluster_cli"
        for result in results for step in result.steps
    )
    if not native:
        return
    require_qualified_feature(
        "native_split_brain_resolution", get_version, "native split-brain resolution"
    )
