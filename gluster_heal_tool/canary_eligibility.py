# SPDX-License-Identifier: GPL-2.0-only
"""Fail-closed eligibility checks for the POSIX canary's saved-state bridge."""
from pathlib import PurePosixPath
from uuid import UUID

from .heal_parser import parse_heal_info_text


POSIX_STATE_VERSION = 1


def validate_posix_source_choice_state(state: object, *, volume: str, scenario: str) -> str:
    """Return the recorded logical path, without discovery or state mutation.

    Passing admits only a harness review plan. Recorded CLI output is checked
    independently of setup booleans; it is not a fresh live execution check.
    """
    def refuse(reason):
        raise RuntimeError(
            f"scenario {scenario!r}: {reason}; retain this state for diagnosis/cleanup, "
            "then collect fresh operator evidence or create a supported fixture"
        )

    def absolute_path(value):
        if (not isinstance(value, str) or not value.startswith("/")
                or any(part in (".", "..") for part in value.split("/"))):
            refuse("missing or invalid absolute path")
        return PurePosixPath(value)

    if not isinstance(state, dict) or state.get("kind") != "file-posix-metadata-split-brain":
        refuse("not a file POSIX metadata split-brain canary")
    if state.get("fixture_scope") == "x4-direct-bookkeeping-diagnostic":
        refuse("direct replica-4 bookkeeping fixture is diagnostic-only and cannot enter source selection")
    if type(state.get("schema_version")) is not int or state["schema_version"] != POSIX_STATE_VERSION:
        refuse("missing or unsupported canary state version")
    if state.get("volume") != volume or state.get("scenario") != scenario:
        refuse("recorded volume/scenario identity does not match the request")
    for field, expected in (("fixture_scope", "x3-upstream-derived-fixture"),
                            ("construction_class", "afr-synthesized"), ("proof_label", "afr-synthesized")):
        if state.get(field) != expected:
            refuse(f"unsupported {field}")
    for field, expected in (("source_choice_eligible", True), ("gluster_visible_metadata_split_brain", True),
                            ("heal_crawl_triggered", True), ("leave_heal_pending", False)):
        if state.get(field) is not expected:
            refuse(f"missing or conflicting {field}; pending-only fixtures are not source-choice evidence")
    if state.get("partial", False) is not False:
        refuse("partial canary construction")

    maps = {}
    for field in ("backend_roots", "backend_by_host", "metadata_backend_by_host", "metadata_tuple_by_host", "brick_roles_by_host"):
        value = state.get(field)
        if not isinstance(value, dict) or len(value) != 3 or any(not isinstance(host, str) or not host.strip() for host in value):
            refuse(f"{field} must record exactly three brick hosts; legacy/unknown replica-4 state is diagnostic-only")
        maps[field] = value
    hosts = set(maps["backend_roots"])
    if any(set(value) != hosts for value in maps.values()):
        refuse("inconsistent per-host topology and metadata maps")
    roles = maps["brick_roles_by_host"]
    if sorted(roles.values(), key=str) not in (["data", "data", "data"], ["arbiter", "data", "data"]):
        refuse("unsupported or unknown brick roles")
    source = state.get("source_host")
    if not isinstance(source, str) or source not in hosts or roles[source] != "data":
        refuse("recorded source must be a known data brick")

    mount_root = absolute_path(state.get("mount_root"))
    mount_file = absolute_path(state.get("mount_file"))
    try:
        logical_path = str(mount_file.relative_to(mount_root))
    except ValueError:
        refuse("mount file is outside the recorded mount root")
    if logical_path == "." or not logical_path.startswith(scenario + "/"):
        refuse("mount file is outside the recorded scenario")
    for host in hosts:
        expected = absolute_path(maps["backend_roots"][host]) / logical_path
        if any(absolute_path(maps[field][host]) != expected for field in ("backend_by_host", "metadata_backend_by_host")):
            refuse(f"backend identity differs from the recorded logical path on {host}")
        record = maps["metadata_tuple_by_host"][host]
        if not isinstance(record, dict):
            refuse(f"missing POSIX metadata tuple on {host}")
        for field in ("mode_bits", "uid", "gid"):
            value = record.get(field)
            if type(value) is not int or value < 0 or field == "mode_bits" and value > 0o7777:
                refuse(f"invalid {field} on {host}")
        if any(not isinstance(record.get(field), str) for field in ("acl_access", "acl_default")):
            refuse(f"missing ACL observation on {host}")

    gfid = state.get("gfid_uuid")
    try:
        canonical_gfid = str(UUID(gfid))
    except (ValueError, TypeError, AttributeError):
        refuse("missing or invalid recorded GFID")
    if canonical_gfid != gfid.lower():
        refuse("recorded GFID is not canonical")
    identities = {"/" + logical_path, f"<gfid:{canonical_gfid}>"}
    for field in ("heal_info_before", "heal_info_after", "heal_info_split_brain_before", "heal_info_split_brain_after"):
        snapshot = state.get(field)
        if not isinstance(snapshot, str) or not snapshot.strip():
            refuse(f"missing {field} snapshot")
        if "split_brain" not in field:
            continue
        # Require a parsed row for this object on one of the recorded bricks.
        # A nonempty header, a same-basename suffix, or a setup flag is insufficient.
        entries = parse_heal_info_text(snapshot)
        matching = [entry for entry in entries if entry.raw in identities
                    and entry.source_host in hosts
                    and entry.brick == maps["backend_roots"][entry.source_host]]
        if not matching:
            refuse(f"{field} has no matching split-brain row on a recorded brick")
    return logical_path
