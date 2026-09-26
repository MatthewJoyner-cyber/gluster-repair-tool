# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Volume helpers."""
from __future__ import annotations

import re
import json
import time
import subprocess

from .controller_paths import default_brick_layout_path
from .controller_paths import default_health_report_path
from .gluster_compat import require_qualified_feature
from .heal_parser import parse_heal_info_text

HEAL_OPTIONS = [
    "cluster.self-heal-daemon",
    "cluster.data-self-heal",
    "cluster.metadata-self-heal",
    "cluster.entry-self-heal",
]


def _run_gluster_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _run_gluster_command_with_sudo_fallback(
    command: list[str],
    *,
    error_prefix: str,
) -> subprocess.CompletedProcess[str]:
    commands = [
        list(command),
        ["sudo", "-n", *command],
    ]
    last_error = ""
    for candidate in commands:
        proc = _run_gluster_command(candidate)
        if proc.returncode == 0:
            return proc
        last_error = proc.stderr.strip() or error_prefix
    raise RuntimeError(last_error)


def get_volume_info(volume: str) -> str:
    proc = _run_gluster_command_with_sudo_fallback(
        ["gluster", "volume", "info", volume],
        error_prefix=f"failed to read gluster volume info for {volume}",
    )
    return proc.stdout


def get_heal_info_text(volume: str) -> str:
    proc = _run_gluster_command_with_sudo_fallback(
        ["gluster", "volume", "heal", volume, "info"],
        error_prefix=f"failed to read gluster heal info for {volume}",
    )
    return proc.stdout


def run_heal(volume: str, *, settle_seconds: float = 5.0) -> None:
    require_qualified_feature("pending_index_heal", get_gluster_version, "pending index heal")
    _run_gluster_command_with_sudo_fallback(
        ["gluster", "volume", "heal", volume],
        error_prefix=f"failed to launch gluster heal for {volume}",
    )
    if settle_seconds > 0:
        time.sleep(settle_seconds)


def run_full_heal(volume: str) -> None:
    require_qualified_feature("full_namespace_heal", get_gluster_version, "full namespace heal")
    _run_gluster_command_with_sudo_fallback(
        ["gluster", "volume", "heal", volume, "full"],
        error_prefix=f"failed to launch full gluster heal for {volume}",
    )


def get_gluster_version() -> str:
    proc = _run_gluster_command_with_sudo_fallback(
        ["gluster", "--version"],
        error_prefix="failed to read gluster version",
    )
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("glusterfs "):
            return line.split(None, 1)[1].strip()
    match = re.search(r"\b(\d+(?:\.\d+)+)\b", proc.stdout)
    if match:
        return match.group(1)
    raise RuntimeError("gluster version was not reported")


def get_heal_statistics(volume: str) -> str:
    proc = _run_gluster_command_with_sudo_fallback(
        ["gluster", "volume", "heal", volume, "statistics", "heal-count"],
        error_prefix=f"failed to read heal statistics for {volume}",
    )
    return proc.stdout


def get_volume_option(volume: str, option: str) -> str:
    proc = _run_gluster_command_with_sudo_fallback(
        ["gluster", "volume", "get", volume, option],
        error_prefix=f"failed to read volume option {option} for {volume}",
    )
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("value:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError(f"volume option {option} was not reported for {volume}")


def set_volume_option(volume: str, option: str, value: str) -> None:
    _run_gluster_command_with_sudo_fallback(
        ["gluster", "volume", "set", volume, option, value],
        error_prefix=f"failed to set volume option {option} for {volume}",
    )


def reset_volume_option(volume: str, option: str) -> None:
    _run_gluster_command_with_sudo_fallback(
        ["gluster", "volume", "reset", volume, option],
        error_prefix=f"failed to reset volume option {option} for {volume}",
    )


def get_heal_entries(volume: str):
    return parse_heal_info_text(get_heal_info_text(volume))


def parse_volume_type(volume_info_text: str) -> str:
    for raw_line in volume_info_text.splitlines():
        line = raw_line.strip()
        if line.startswith("Type:"):
            return line.split(":", 1)[1].strip()
    return ""


def _parse_brick_entry(raw_line: str) -> tuple[str, str, str] | None:
    line = raw_line.strip()
    if not line.lower().startswith("brick"):
        return None
    if ":" not in line:
        return None
    _, payload = line.split(":", 1)
    host, _, path = payload.strip().partition(":")
    host = host.strip()
    path = path.strip()
    role = "arbiter" if path.endswith(" (arbiter)") else "data"
    if role == "arbiter":
        path = path[: -len(" (arbiter)")].rstrip()
    if not host or not path:
        return None
    return host, path, role


def parse_bricks(volume_info_text: str) -> list[tuple[str, str]]:
    bricks: list[tuple[str, str]] = []
    for raw_line in volume_info_text.splitlines():
        entry = _parse_brick_entry(raw_line)
        if entry is None:
            continue
        host, path, _role = entry
        bricks.append((host, path))
    return bricks


def parse_brick_roles(volume_info_text: str) -> dict[str, str]:
    brick_roles: dict[str, str] = {}
    for raw_line in volume_info_text.splitlines():
        entry = _parse_brick_entry(raw_line)
        if entry is None:
            continue
        host, _path, role = entry
        existing = brick_roles.get(host)
        if existing is None:
            brick_roles[host] = role
            continue
        if existing != role:
            raise RuntimeError(f"expected a single brick role per host, found both {existing!r} and {role!r} for {host}")
    if not brick_roles:
        raise RuntimeError("no brick roles found in gluster volume info")
    return brick_roles


def parse_brick_hosts(volume_info_text: str) -> list[str]:
    hosts: list[str] = []
    for host, _path in parse_bricks(volume_info_text):
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def normalize_host_alias(value: str) -> str:
    return value.strip().rstrip(".").lower()


def parse_common_brick_path(volume_info_text: str) -> str:
    bricks = parse_bricks(volume_info_text)
    paths = {path for _host, path in bricks}
    if not paths:
        raise RuntimeError("no brick paths found in gluster volume info")
    if len(paths) != 1:
        raise RuntimeError(f"expected a single common brick path, found: {sorted(paths)}")
    return next(iter(paths))


def parse_brick_paths(volume_info_text: str) -> dict[str, str]:
    brick_paths: dict[str, str] = {}
    for raw_line in volume_info_text.splitlines():
        entry = _parse_brick_entry(raw_line)
        if entry is None:
            continue
        host, path, _role = entry
        existing = brick_paths.get(host)
        if existing is None:
            brick_paths[host] = path
            continue
        if existing != path:
            raise RuntimeError(
                f"expected a single brick path per host, found both {existing!r} and {path!r} for {host}"
            )
    if not brick_paths:
        raise RuntimeError("no brick paths found in gluster volume info")
    return brick_paths

def _load_cached_brick_paths_payload(report: dict[str, object]) -> dict[str, str]:
    brick_paths: dict[str, str] = {}
    host_facts = {
        normalize_host_alias(str(item.get("host") or "").strip()): item
        for item in report.get("host_facts") or []
        if isinstance(item, dict) and str(item.get("host") or "").strip()
    }
    for item in report.get("bricks") or []:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or "").strip()
        path = str(item.get("path") or "").strip()
        if not host or not path:
            continue
        aliases = {host}
        aliases.update(str(alias).strip() for alias in item.get("aliases") or [] if str(alias).strip())
        fact = host_facts.get(normalize_host_alias(host))
        if isinstance(fact, dict):
            aliases.update(
                str(alias).strip()
                for alias in fact.get("aliases") or []
                if str(alias).strip()
            )
            for key in ("hostname", "fqdn"):
                alias = str(fact.get(key) or "").strip()
                if alias:
                    aliases.add(alias)
            for ip in fact.get("ips") or []:
                alias = str(ip).strip()
                if alias:
                    aliases.add(alias)
        for alias in aliases:
            normalized = normalize_host_alias(alias)
            if not normalized:
                continue
            existing = brick_paths.get(normalized)
            if existing is None:
                brick_paths[normalized] = path
                continue
            if existing != path:
                raise RuntimeError(
                    f"expected a single brick path for host alias {alias!r}, found both {existing!r} and {path!r}"
                )
    return brick_paths


def _load_cached_brick_paths(volume: str) -> dict[str, str]:
    for report_path in (default_brick_layout_path(volume), default_health_report_path(volume)):
        if not report_path.exists():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        brick_paths = _load_cached_brick_paths_payload(report if isinstance(report, dict) else {})
        if brick_paths:
            return brick_paths
    return {}


def ensure_supported_volume_type(volume: str, volume_info_text: str) -> None:
    volume_type = parse_volume_type(volume_info_text)
    if volume_type != "Replicate":
        raise RuntimeError(
            f"unsupported volume type for this tool: {volume!r} is {volume_type or 'unknown'}; only pure Replicate volumes are supported"
        )


def discover_brick_hosts(volume: str) -> list[str]:
    info = get_volume_info(volume)
    ensure_supported_volume_type(volume, info)
    return parse_brick_hosts(info)


def discover_common_brick_path(volume: str) -> str:
    info = get_volume_info(volume)
    ensure_supported_volume_type(volume, info)
    return parse_common_brick_path(info)


def discover_brick_paths(volume: str) -> dict[str, str]:
    try:
        info = get_volume_info(volume)
    except RuntimeError:
        cached = _load_cached_brick_paths(volume)
        if cached:
            return cached
        raise
    ensure_supported_volume_type(volume, info)
    return {
        normalize_host_alias(host): path
        for host, path in parse_brick_paths(info).items()
    }


def get_volume_option(volume: str, option: str) -> str:
    proc = _run_gluster_command_with_sudo_fallback(
        ["gluster", "volume", "get", volume, option],
        error_prefix=f"failed to read gluster volume option {option!r} for {volume}",
    )
    for raw_line in reversed(proc.stdout.splitlines()):
        line = raw_line.strip()
        if not line or line.startswith("Option") or line.startswith("Value"):
            continue
        if option in line:
            parts = line.split()
            if parts:
                return parts[-1].strip()
    raise RuntimeError(f"unable to parse gluster volume get output for {option!r}")


def get_heal_settings(volume: str) -> dict[str, str]:
    info = get_volume_info(volume)
    ensure_supported_volume_type(volume, info)
    return {option: get_volume_option(volume, option) for option in HEAL_OPTIONS}


def set_volume_option(volume: str, option: str, value: str) -> None:
    _run_gluster_command_with_sudo_fallback(
        ["gluster", "volume", "set", volume, option, value],
        error_prefix=f"failed to set gluster volume option {option!r}={value!r} for {volume}",
    )


def set_heal_settings(volume: str, enabled: bool) -> dict[str, str]:
    value = "on" if enabled else "off"
    for option in HEAL_OPTIONS:
        set_volume_option(volume, option, value)
    return get_heal_settings(volume)


class HealSettingsRestoreError(RuntimeError):
    """Raised when Gluster does not return to the captured heal configuration."""


def set_heal_settings_exact(volume: str, settings: dict[str, str]) -> dict[str, str]:
    for option in HEAL_OPTIONS:
        if option not in settings:
            raise HealSettingsRestoreError(f"missing heal setting for {option!r} when restoring {volume}")
        set_volume_option(volume, option, settings[option])
    restored = get_heal_settings(volume)
    expected_effective = summarize_heal_settings(volume, settings).get("effective_settings", {})
    restored_effective = summarize_heal_settings(volume, restored).get("effective_settings", {})
    mismatches = [
        f"{option}: expected {expected_effective.get(option, '')!r}, got {restored_effective.get(option, '')!r}"
        for option in HEAL_OPTIONS
        if expected_effective.get(option) != restored_effective.get(option)
    ]
    if mismatches:
        raise HealSettingsRestoreError(
            f"heal settings were not restored for {volume}: {', '.join(mismatches)}"
        )
    return restored


def summarize_heal_settings(volume: str, settings: dict[str, str]) -> dict[str, object]:
    normalized: dict[str, str] = {}
    effective: dict[str, str] = {}
    for key, value in settings.items():
        lowered = value.lower()
        if key == "cluster.self-heal-daemon":
            lowered = {"enable": "on", "disable": "off"}.get(lowered, lowered)
        normalized[key] = lowered
        effective[key] = "on" if lowered == "(default)" else lowered
    all_on = all(value == "on" for value in normalized.values())
    all_on_effective = all(value == "on" for value in effective.values())
    all_off = all(value == "off" for value in effective.values())
    warnings: list[str] = []
    if all_off:
        warnings.append("heal-related volume options are off")
        warnings.append("directory/file name-heal cannot be disabled and may still occur on access")
    elif all_on_effective:
        warnings.append("heal-related volume options are on")
    else:
        warnings.append("heal-related volume options are in a mixed state")
        warnings.append("directory/file name-heal cannot be disabled and may still occur on access")
    if effective.get("cluster.self-heal-daemon") == "off":
        warnings.append(
            "cluster.self-heal-daemon is off; split-brain and below-quorum states may stay hidden until heal is enabled"
        )
    return {
        "volume": volume,
        "settings": settings,
        "effective_settings": effective,
        "all_on": all_on_effective,
        "all_off": all_off,
        "warnings": warnings,
    }
