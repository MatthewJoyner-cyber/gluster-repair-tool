# SPDX-License-Identifier: GPL-2.0-only
"""Prepare a reviewable, local text-only maintainer handoff from saved evidence."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
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
IPV4_PATTERN = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6_PATTERN = re.compile(r"(?<![\w:])(?=[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:)[0-9A-Fa-f:]{3,}(?![\w:])")
BRICK_HOST_PATTERN = re.compile(r"(?im)^\s*Brick\s+([A-Za-z0-9._-]+):/")
HOST_FIELD_PATTERN = re.compile(r'''(?im)(?:^|[,{])\s*["']?(?:Hostname|Host|source_host)["']?\s*[:=]\s*["']?([A-Za-z0-9._-]+)''')
CREDENTIAL_PATTERN = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*(?!\[REDACTED\])\S+"
)
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")
FILE_CONTENT_KEYS = frozenset({
    "content", "file_content", "file_contents", "content_bytes",
    "payload_bytes", "content_b64", "data_b64", "base64_payload",
})
FILE_CONTENT_FIELD = re.compile(
    r"(?im)^\s*(?:file[-_ ]?contents?|payload[-_ ]?bytes?|content[-_ ]?b64|data[-_ ]?b64)\s*[:=]"
)
DOCUMENTATION_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
))
ALIAS_CATEGORIES = frozenset({"server", "organization", "ip", "person", "account", "path"})


def _real_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (address.is_loopback or address.is_unspecified or any(
        address in network for network in DOCUMENTATION_NETWORKS
    ))


def _contains_file_content(content: str) -> bool:
    if FILE_CONTENT_FIELD.search(content):
        return True
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
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

    return visit(parsed)


def _aliases(identifiers: list[str], contents: dict[str, str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    counts: dict[str, int] = {}
    for item in identifiers:
        category, separator, value = item.partition(":")
        if separator and category in ALIAS_CATEGORIES | {"secret"}:
            value = value.strip()
            if not value:
                raise ValueError("private identifier entry has an empty value")
            if value in aliases:
                raise ValueError("duplicate private identifier entry")
            if category == "secret":
                aliases[value] = "[REDACTED]"
            else:
                counts[category] = counts.get(category, 0) + 1
                aliases[value] = f"{category}{counts[category]}"
        else:
            if item in aliases:
                raise ValueError("duplicate private identifier entry")
            aliases[item] = "[REDACTED]"
    for content in contents.values():
        for pattern in (BRICK_HOST_PATTERN, HOST_FIELD_PATTERN):
            for match in pattern.finditer(content):
                host = match.group(1)
                if host.lower() != "localhost" and host not in aliases and not _real_ip(host):
                    counts["server"] = counts.get("server", 0) + 1
                    aliases[host] = f"server{counts['server']}"
        for pattern in (IPV4_PATTERN, IPV6_PATTERN):
            for match in pattern.finditer(content):
                address = match.group()
                if _real_ip(address) and address not in aliases:
                    counts["ip"] = counts.get("ip", 0) + 1
                    aliases[address] = f"ip{counts['ip']}"
    return aliases


def _anonymize(content: str, aliases: dict[str, str]) -> str:
    values = sorted(aliases, key=len, reverse=True)
    if values:
        pattern = re.compile(r"(?<![\w.-])(?:" + "|".join(map(re.escape, values)) + r")(?![\w.-])")
        content = pattern.sub(lambda match: aliases[match.group()], content)
    return PRIVATE_PATTERN.sub("[REDACTED]", content)


def _has_unlisted_sensitive_value(content: str) -> bool:
    if any(pattern.search(content) for pattern in (
        PRIVATE_PATTERN, CREDENTIAL_PATTERN, URL_PATTERN, PRIVATE_KEY_PATTERN,
    )):
        return True
    for pattern in (BRICK_HOST_PATTERN, HOST_FIELD_PATTERN):
        for match in pattern.finditer(content):
            host = match.group(1)
            if host.lower() != "localhost" and not re.fullmatch(r"(?:server|ip)\d+", host):
                return True
    for pattern in (IPV4_PATTERN, IPV6_PATTERN):
        for match in pattern.finditer(content):
            if _real_ip(match.group()):
                return True
    return False


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
    """Copy only present text evidence; fail closed on unsafe input.

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
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ValueError(f"{name} cannot be opened as a regular artifact") from exc
        with os.fdopen(descriptor, "rb") as input_file:
            details = os.fstat(input_file.fileno())
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"{name} is not a regular, non-symlink file")
            if details.st_size > MAX_TEXT_BYTES:
                raise ValueError(f"{name} exceeds text artifact size limit")
            raw = input_file.read(MAX_TEXT_BYTES + 1)
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
    aliases = _aliases(identifiers, raw_contents)
    for name, raw_content in raw_contents.items():
        content = _anonymize(raw_content, aliases)
        if _has_unlisted_sensitive_value(content):
            raise ValueError(f"unlisted private or sensitive value remains in {name}")
        prepared[name] = content
        inventory[name] = {
            "status": "copied_redacted",
            "file": f"{name}.txt",
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    destination.mkdir(mode=0o700)
    for name, content in prepared.items():
        _write_private_text(destination / f"{name}.txt", content)
    _write_private_text(
        destination / "inventory.json",
        json.dumps(inventory, indent=2, sort_keys=True) + "\n",
    )
    collected = [name for name, item in inventory.items() if item["status"] == "copied_redacted"]
    missing = [name for name, item in inventory.items() if item["status"] == "missing"]
    draft = (
        "Gluster Repair Tool maintainer evidence handoff (not sent)\n"
        "Observed evidence: " + (", ".join(collected) or "none") + "\n"
        "Missing requested evidence: " + (", ".join(missing) or "none") + "\n"
        "Tool classifications are proposals, not proof of a broken GFID handle.\n"
        "Hypotheses and safe lab-control design require separate operator review.\n"
        "Review every copied file and inventory before choosing what to share with the tool maintainers.\n"
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
