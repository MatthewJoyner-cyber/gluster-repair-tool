# SPDX-License-Identifier: GPL-2.0-only
"""Prepare a reviewable, local text-only support draft from existing evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


ALLOWED_ARTIFACTS = frozenset({
    "heal_info", "volume_info", "volume_status", "brick_roles", "afr_inspection",
    "resolver_record", "status", "health", "manifest", "observations", "plan",
    "apply", "execute_results", "decisions", "summary", "assistants",
})
MAX_TEXT_BYTES = 8 * 1024 * 1024
PRIVATE_PATTERN = re.compile(
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|/(?:g?home)/[^/\s]+"
)


def prepare_support_bundle(
    destination: Path,
    artifacts: dict[str, Path],
    *,
    private_identifiers: list[str],
) -> dict[str, object]:
    """Copy only present text evidence; fail closed on unsafe input.

    The caller supplies already-collected bounded artifacts. This never probes a
    brick, starts a heal crawl, or claims a case was submitted.
    """
    destination = Path(destination)
    if destination.exists():
        raise ValueError(f"bundle destination already exists: {destination}")
    unknown = set(artifacts) - ALLOWED_ARTIFACTS
    if unknown:
        raise ValueError(f"unsupported artifact labels: {', '.join(sorted(unknown))}")
    identifiers = [value for value in private_identifiers if value]
    if not identifiers:
        raise ValueError("at least one private identifier is required for bundle validation")
    prepared: dict[str, str] = {}
    inventory: dict[str, dict[str, str]] = {}
    for name, source in sorted(artifacts.items()):
        source = Path(source)
        if source.is_symlink():
            raise ValueError(f"{name} is a symlink")
        if not source.exists():
            inventory[name] = {"status": "missing"}
            continue
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"{name} is not a regular, non-symlink file")
        if source.stat().st_size > MAX_TEXT_BYTES:
            raise ValueError(f"{name} exceeds text artifact size limit")
        try:
            content = source.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise ValueError(f"{name} is not UTF-8 text") from exc
        for identifier in identifiers:
            content = content.replace(identifier, "[REDACTED]")
        if PRIVATE_PATTERN.search(content):
            raise ValueError(f"unlisted private identifier remains in {name}")
        prepared[name] = content
        inventory[name] = {
            "status": "copied_redacted",
            "file": f"{name}.txt",
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    destination.mkdir(parents=True)
    for name, content in prepared.items():
        (destination / f"{name}.txt").write_text(content, encoding="utf-8")
    (destination / "inventory.json").write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    collected = [name for name, item in inventory.items() if item["status"] == "copied_redacted"]
    missing = [name for name, item in inventory.items() if item["status"] == "missing"]
    draft = (
        "Gluster support case draft (not submitted)\n"
        "Observed evidence: " + (", ".join(collected) or "none") + "\n"
        "Missing requested evidence: " + (", ".join(missing) or "none") + "\n"
        "Tool classifications are proposals, not proof of a broken GFID handle.\n"
        "Hypotheses and safe lab-control design require separate operator review.\n"
        "Review every copied file and inventory before attaching or submitting.\n"
    )
    (destination / "submission-draft.txt").write_text(draft, encoding="utf-8")
    return {"inventory": inventory, "draft": draft}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--artifact", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--private-identifiers", required=True, type=Path,
                        help="private file with one literal identifier per line")
    args = parser.parse_args()
    artifacts: dict[str, Path] = {}
    for item in args.artifact:
        name, separator, path = item.partition("=")
        if not separator or not path or name in artifacts:
            parser.error("each --artifact must be a unique NAME=PATH")
        artifacts[name] = Path(path)
    identifiers = [line.strip() for line in args.private_identifiers.read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.lstrip().startswith("#")]
    prepare_support_bundle(args.destination, artifacts, private_identifiers=identifiers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
