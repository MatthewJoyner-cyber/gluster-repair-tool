# SPDX-License-Identifier: GPL-2.0-only
"""Prepare a reviewable, local metadata-only maintainer handoff from saved evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .diagnostic_metadata import MetadataExporter


ALLOWED_ARTIFACTS = frozenset({
    "heal_info", "volume_info", "volume_status", "brick_roles", "afr_inspection",
    "resolver_record", "status", "health", "manifest", "observations", "plan",
    "apply", "execute_results", "decisions", "summary", "assistants",
})
MAX_TEXT_BYTES = 8 * 1024 * 1024
FILE_CONTENT_KEYS = frozenset({
    "content", "file_content", "file_contents", "content_bytes",
    "payload_bytes", "content_b64", "data_b64", "base64_payload",
})
FILE_CONTENT_FIELD = re.compile(
    r"(?im)^\s*(?:file[-_ ]?contents?|payload[-_ ]?bytes?|content[-_ ]?b64|data[-_ ]?b64)\s*[:=]"
)
IDENTIFIER_TYPES = frozenset({"server", "organization", "ip", "person", "account", "path", "secret"})


def _private_values(identifiers: list[str]) -> list[str]:
    values = []
    for item in identifiers:
        category, separator, value = item.partition(":")
        value = value.strip() if separator and category in IDENTIFIER_TYPES else item
        if not value:
            raise ValueError("private identifier entry has an empty value")
        if value in values:
            raise ValueError("duplicate private identifier entry")
        values.append(value)
    return values


def _contains_file_content(content: str) -> bool:
    if FILE_CONTENT_FIELD.search(content):
        return True
    try:
        parsed = json.loads(content)
    except (ValueError, RecursionError):
        return False

    def visit(value: object) -> bool:
        if isinstance(value, dict):
            return any(
                str(key).casefold() in FILE_CONTENT_KEYS or visit(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(visit(item) for item in value)
        return False

    try:
        return visit(parsed)
    except RecursionError as exc:
        raise ValueError("artifact nesting exceeds metadata limits") from exc


def _write_private_text(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as output:
        output.write(content)


def prepare_support_bundle(
    destination: Path,
    artifacts: dict[str, Path],
    *,
    private_identifiers: list[str],
) -> dict[str, object]:
    """Export defined metadata fields from present evidence; omit unknown formats.

    The caller supplies already-collected bounded artifacts. This never probes a
    brick, starts a heal crawl, or sends the bundle anywhere.
    """
    destination = Path(destination)
    if destination.exists():
        raise ValueError(f"bundle destination already exists: {destination}")
    unknown = set(artifacts) - ALLOWED_ARTIFACTS
    if unknown:
        raise ValueError(f"unsupported artifact labels: {', '.join(sorted(unknown))}")
    identifiers = [value.strip() for value in private_identifiers if value.strip()]
    if not identifiers:
        raise ValueError("at least one private identifier is required for bundle validation")
    prepared: dict[str, str] = {}
    inventory: dict[str, dict[str, str]] = {}
    raw_contents: dict[str, str] = {}
    for name, source in sorted(artifacts.items()):
        source = Path(source)
        if source.is_symlink():
            raise ValueError(f"{name} is a symlink")
        if not source.exists():
            inventory[name] = {"status": "missing"}
            continue
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise ValueError(f"{name} cannot be opened as a regular artifact") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"{name} is not a regular, non-symlink file")
            if details.st_size > MAX_TEXT_BYTES:
                raise ValueError(f"{name} exceeds text artifact size limit")
            with os.fdopen(descriptor, "rb", closefd=False) as input_file:
                raw = input_file.read(MAX_TEXT_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > MAX_TEXT_BYTES:
            raise ValueError(f"{name} exceeds text artifact size limit")
        try:
            content = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError(f"{name} is not UTF-8 text") from exc
        if "\x00" in content:
            raise ValueError(f"{name} is not plain text metadata")
        if _contains_file_content(content):
            raise ValueError(f"{name} contains file-content fields; supply metadata-only evidence")
        raw_contents[name] = content
    exporter = MetadataExporter(private_values=_private_values(identifiers))
    for name, raw_content in raw_contents.items():
        projected = exporter.export(name, raw_content)
        if projected is None:
            inventory[name] = {"status": "omitted_unsupported_format"}
            continue
        content = json.dumps(projected, indent=2, sort_keys=True) + "\n"
        prepared[name] = content
        inventory[name] = {
            "status": "exported_metadata",
            "file": f"{name}.json",
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    destination.mkdir(mode=0o700)
    for name, content in prepared.items():
        _write_private_text(destination / f"{name}.json", content)
    _write_private_text(
        destination / "inventory.json",
        json.dumps(inventory, indent=2, sort_keys=True) + "\n",
    )
    collected = [name for name, item in inventory.items() if item["status"] == "exported_metadata"]
    missing = [name for name, item in inventory.items() if item["status"] == "missing"]
    omitted = [name for name, item in inventory.items() if item["status"] == "omitted_unsupported_format"]
    draft = (
        "Gluster Repair Tool maintainer evidence handoff (not sent)\n"
        "Observed evidence: " + (", ".join(collected) or "none") + "\n"
        "Missing requested evidence: " + (", ".join(missing) or "none") + "\n"
        "Omitted unsupported evidence: " + (", ".join(omitted) or "none") + "\n"
        "Metadata projections omit unknown fields and replace names with bundle-local aliases.\n"
        "Tool classifications are proposals, not proof of a broken GFID handle.\n"
        "Hypotheses and safe lab-control design require separate operator review.\n"
        "Review every exported file and inventory before choosing what to share with the tool maintainers.\n"
    )
    _write_private_text(destination / "maintainer-handoff.txt", draft)
    return {"inventory": inventory, "draft": draft}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--artifact", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--private-identifiers", required=True, type=Path,
                        help="private file with one identifier per line; optional server:/organization:/person:/account:/path:/ip:/secret: types")
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
