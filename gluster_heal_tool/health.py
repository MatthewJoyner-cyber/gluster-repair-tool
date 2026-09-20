# SPDX-License-Identifier: GPL-2.0-only
"""Gluster volume health-check helpers."""
from __future__ import annotations

import json
import ipaddress
import re
import shlex
import subprocess
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .install_paths import (
    DEFAULT_HOST_OPS_PATH,
    DEFAULT_LOG_OPS_PATH,
    DEFAULT_SERVICE_USER,
    DEFAULT_WORKER_PATH,
)
from .controller_paths import default_brick_layout_path
from .remote_ops import ssh_remote_command
from .shared_io import write_json_shared
from .volume import (
    ensure_supported_volume_type,
    get_heal_settings,
    get_volume_info,
    parse_bricks,
    parse_brick_hosts,
    parse_volume_type,
    summarize_heal_settings,
)

HEALTH_REPORT_STALE_SECONDS = 600
_HEAL_LOCK_SKIP_PATTERN = r"only [0-9]+ sub-volumes could be locked in .*:self-heal domain"
_GLUSTER_LOG_TS_RE = re.compile(r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ [+-]\d{4})\]")

_STATUS_BRICK_RE = re.compile(
    r"^Brick\s+(?P<host>[^:]+):(?P<path>\S+)\s+\S+\s+\S+\s+(?P<online>[YN])\s+(?P<pid>\S+)"
)
_STATUS_SHD_RE = re.compile(
    r"^Self-heal Daemon on\s+(?P<host>\S+)\s+\S+\s+\S+\s+(?P<online>[YN])\s+(?P<pid>\S+)"
)


def _now_iso() -> str:
    return _utc_now().isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_gluster_log_timestamp(line: str) -> datetime | None:
    match = _GLUSTER_LOG_TS_RE.search(line)
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S.%f %z")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def _run_local(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _run_local_with_sudo_fallback(command: list[str]) -> subprocess.CompletedProcess[str]:
    commands = [list(command), ["sudo", "-n", *command]]
    last_proc = None
    for candidate in commands:
        proc = _run_local(candidate)
        last_proc = proc
        if proc.returncode == 0:
            return proc
    return last_proc or subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="")


def _volume_status_text(volume: str) -> str:
    proc = _run_local_with_sudo_fallback(["gluster", "volume", "status", volume])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"failed to read gluster volume status for {volume}")
    return proc.stdout


def _snapshot_inventory_text(volume: str) -> tuple[bool, str, str]:
    proc = _run_local_with_sudo_fallback(["gluster", "snapshot", "list", volume])
    ok = proc.returncode == 0
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    return ok, stdout, stderr


def _parse_snapshot_names(text: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith(("volume name", "snapshot list", "snapshot id", "status", "type", "bricks", "brick")):
            continue
        if lower.startswith("no snapshot"):
            return []
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip().lower() in {"snapshot name", "name"}:
                candidate = value.strip().split()[0] if value.strip() else ""
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    names.append(candidate)
                continue
        candidate = line.split()[0]
        if candidate and candidate.lower() not in {"snapshot", "volume", "name"} and candidate not in seen:
            seen.add(candidate)
            names.append(candidate)
    return names


def _parse_volume_status(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pending: str = ""

    def flush_pending() -> None:
        nonlocal pending
        line = pending.strip()
        pending = ""
        if not line.startswith("Brick "):
            return
        match = _STATUS_BRICK_RE.match(line)
        if not match:
            return
        pid = match.group("pid")
        rows.append(
            {
                "host": match.group("host"),
                "path": match.group("path"),
                "online": match.group("online") == "Y",
                "pid": pid,
                "pid_running": pid not in {"", "-", "N/A", "0"},
                "raw": line,
            }
        )

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Brick "):
            flush_pending()
            pending = line
            continue
        if pending and not line.startswith(("Self-heal Daemon", "Task Status of Volume", "Status of volume:", "Gluster process", "Brick ")):
            pending += line
            continue
        flush_pending()
    flush_pending()
    return rows


def _parse_self_heal_daemon_status(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pending = ""

    def flush_pending() -> None:
        nonlocal pending
        line = pending.strip()
        pending = ""
        match = _STATUS_SHD_RE.match(line)
        if not match:
            return
        pid = match.group("pid")
        rows.append(
            {
                "host": match.group("host"),
                "online": match.group("online") == "Y",
                "pid": pid,
                "pid_running": pid not in {"", "-", "N/A", "0"},
                "source": "text",
            }
        )

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Self-heal Daemon on "):
            flush_pending()
            pending = line
            continue
        if pending and not line.startswith(("Brick ", "Task Status of Volume", "Status of volume:", "Gluster process")):
            pending += " " + line
            continue
        flush_pending()
    flush_pending()
    return rows


def _volume_status_shd_xml(volume: str) -> tuple[bool, list[dict[str, Any]], str]:
    """Read the volume-attached SHD table from Gluster's structured status output."""
    proc = _run_local_with_sudo_fallback(["gluster", "--xml", "volume", "status", volume, "shd"])
    if proc.returncode != 0:
        return False, [], proc.stderr.strip() or "structured SHD status is unavailable"
    try:
        root = ET.fromstring(proc.stdout)
    except ET.ParseError as exc:
        return False, [], f"invalid structured SHD status: {exc}"
    if (root.findtext("opRet") or "").strip() != "0":
        return False, [], (root.findtext("opErrstr") or "structured SHD status failed").strip()
    rows: list[dict[str, Any]] = []
    for node in root.findall(".//volStatus/volumes/volume/node"):
        if (node.findtext("hostname") or "").strip() != "Self-heal Daemon":
            continue
        pid = (node.findtext("pid") or "").strip()
        rows.append(
            {
                "host": (node.findtext("path") or "").strip(),
                "online": (node.findtext("status") or "").strip() == "1",
                "pid": pid,
                "pid_running": pid not in {"", "-", "N/A", "0"},
                "source": "xml",
            }
        )
    return True, rows, ""


def _host_aliases(*values: object) -> set[str]:
    return {
        str(value).strip().lower()
        for value in values
        if value is not None and str(value).strip()
    }


def _required_shd_hosts(bricks: list[tuple[str, str]], host_facts: list[dict[str, Any]]) -> list[set[str]]:
    """Collapse alias-equivalent brick endpoints into physical SHD requirements."""
    aliases_by_host: dict[str, set[str]] = {}
    for host, _path in bricks:
        aliases_by_host.setdefault(host, _host_aliases(host))
    for fact in host_facts:
        host = str(fact.get("host") or "").strip()
        if not host:
            continue
        aliases_by_host.setdefault(host, _host_aliases(host)).update(
            _host_aliases(host, fact.get("short_hostname"), fact.get("fqdn"), *(fact.get("ips") or []), *(fact.get("aliases") or []))
        )
    groups: list[set[str]] = []
    for aliases in aliases_by_host.values():
        matches = [group for group in groups if group & aliases]
        if not matches:
            groups.append(set(aliases))
            continue
        merged = set(aliases)
        for group in matches:
            merged.update(group)
            groups.remove(group)
        groups.append(merged)
    return groups


def _self_heal_daemon_check(
    rows: list[dict[str, Any]],
    *,
    bricks: list[tuple[str, str]],
    host_facts: list[dict[str, Any]],
    required: bool,
) -> dict[str, Any]:
    groups = _required_shd_hosts(bricks, host_facts)
    local_aliases = _host_aliases("localhost", socket.gethostname(), socket.getfqdn())
    matched_online: set[int] = set()
    offline_hosts: list[str] = []
    unmatched_hosts: list[str] = []
    for row in rows:
        aliases = _host_aliases(row.get("host"))
        if "localhost" in aliases:
            aliases.update(local_aliases)
        matches = [index for index, group in enumerate(groups) if group & aliases]
        if len(matches) != 1:
            unmatched_hosts.append(str(row.get("host") or "unknown"))
            continue
        if row.get("online") and row.get("pid_running"):
            matched_online.add(matches[0])
        else:
            offline_hosts.append(str(row.get("host") or "unknown"))
    missing = [sorted(group)[0] for index, group in enumerate(groups) if index not in matched_online]
    ok = not offline_hosts and (not required or (not missing and not unmatched_hosts))
    messages: list[str] = []
    if offline_hosts:
        messages.append("self-heal daemon reported offline in volume status: " + ", ".join(offline_hosts))
    if required and missing:
        messages.append("self-heal daemon missing for physical brick host: " + ", ".join(missing))
    if required and unmatched_hosts:
        messages.append("self-heal daemon has no unambiguous brick-host attachment: " + ", ".join(unmatched_hosts))
    return {"kind": "self_heal_daemon_status", "ok": ok, "message": "; ".join(messages), "rows": rows, "required_hosts": [sorted(group) for group in groups], "missing_hosts": missing, "unmatched_hosts": unmatched_hosts}


def _run_remote_check(
    host: str,
    command: list[str],
    *,
    ssh_user: str,
    connect_timeout: float,
    use_sudo: bool = False,
    sudo_path: str | None = None,
    ok_returncodes: tuple[int, ...] = (0,),
) -> dict[str, Any]:
    proc = subprocess.run(
        ssh_remote_command(
            host,
            command,
            ssh_user=ssh_user,
            use_sudo=use_sudo,
            sudo_path=sudo_path,
            connect_timeout=connect_timeout,
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    ok = proc.returncode in ok_returncodes
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "command": ssh_remote_command(
            host,
            command,
            ssh_user=ssh_user,
            use_sudo=use_sudo,
            sudo_path=sudo_path,
            connect_timeout=connect_timeout,
        ),
    }


def _remote_file_check(
    host: str,
    path: str,
    *,
    ssh_user: str,
    connect_timeout: float,
    label: str,
    use_sudo: bool = False,
    sudo_path: str | None = None,
) -> dict[str, Any]:
    result = _run_remote_check(
        host,
        ["test", "-x", path],
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        use_sudo=use_sudo,
        sudo_path=sudo_path,
    )
    result["kind"] = label
    result["host"] = host
    result["path"] = path
    result["message"] = "" if result["ok"] else result["stderr"] or f"{path} is not executable"
    return result


def _remote_service_status_check(
    host: str,
    *,
    ssh_user: str,
    connect_timeout: float,
    service: str,
) -> dict[str, Any]:
    result = _run_remote_check(
        host,
        ["systemctl", "is-active", service],
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        use_sudo=False,
    )
    result["kind"] = f"service_status_{service}"
    result["host"] = host
    result["service"] = service
    result["message"] = "" if result["ok"] else result["stderr"] or f"{service} is not active"
    return result


def _parse_load_average(text: str) -> float | None:
    match = re.search(r"load averages?:\s*([0-9]+(?:\.[0-9]+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_int_stdout(text: str, *, default: int = 0) -> int:
    try:
        return int(str(text).strip().split()[0])
    except (ValueError, IndexError):
        return default


def _remote_heal_activity_check(
    host: str,
    *,
    volume: str,
    ssh_user: str,
    connect_timeout: float,
) -> dict[str, Any]:
    load_result = _run_remote_check(
        host,
        ["uptime"],
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        use_sudo=False,
    )
    cpu_result = _run_remote_check(
        host,
        ["nproc"],
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        use_sudo=False,
    )
    result = {
        "kind": "heal_activity",
        "host": host,
        "ok": bool(load_result.get("ok")) and bool(cpu_result.get("ok")),
        "load1": 0.0,
        "cpu_count": 0,
        "load_threshold": 0.0,
        "load_ratio": 0.0,
        "glusterfsd_cpu": 0.0,
        "inspected_glusterfsd": False,
        "busy": False,
        "message": "",
    }
    load1 = _parse_load_average(str(load_result.get("stdout") or ""))
    if load1 is not None:
        result["load1"] = load1
    cpu_count = _parse_int_stdout(str(cpu_result.get("stdout") or ""), default=0)
    if cpu_count > 0:
        result["cpu_count"] = cpu_count
    result["load_threshold"] = float(result["cpu_count"]) * 0.8 if result["cpu_count"] else 0.0
    if result["cpu_count"] > 0:
        result["load_ratio"] = result["load1"] / float(result["cpu_count"])
    if not result["ok"]:
        result["message"] = str(load_result.get("stderr") or cpu_result.get("stderr") or "unable to inspect heal activity")
        return result
    if result["cpu_count"] <= 0:
        result["ok"] = False
        result["message"] = "unable to determine CPU count for heal activity check"
        return result
    if result["load1"] < result["load_threshold"]:
        return result
    glusterfsd_result = _run_remote_check(
        host,
        ["ps", "-C", "glusterfsd", "-o", "%cpu=", "--no-headers"],
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        use_sudo=False,
    )
    result["inspected_glusterfsd"] = True
    result["ok"] = bool(glusterfsd_result.get("ok"))
    if not result["ok"]:
        result["message"] = str(glusterfsd_result.get("stderr") or "unable to inspect glusterfsd activity")
        return result
    cpu_samples: list[float] = []
    for raw_line in str(glusterfsd_result.get("stdout") or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            cpu_samples.append(float(line))
        except ValueError:
            continue
    result["glusterfsd_cpu"] = sum(cpu_samples)
    if result["glusterfsd_cpu"] >= 50.0:
        result["busy"] = True
        result["message"] = (
            f"gluster heal in progress on {host} for volume {volume}; wait for `gluster volume heal <vol> statistics heal-count` to settle "
            f"before rerunning health-check or trying gluster-repair (load1={result['load1']:.2f}, "
            f"cpu_count={result['cpu_count']}, glusterfsd cpu={result['glusterfsd_cpu']:.2f}%)"
        )
    return result


def _remote_heal_lock_skip_probe(
    host: str,
    *,
    volume: str,
    ssh_user: str,
    connect_timeout: float,
) -> dict[str, Any]:
    result = _run_remote_check(
        host,
        [
            "tail",
            "--volume",
            volume,
            "--lines",
            "1000",
        ],
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        use_sudo=True,
        sudo_path=str(DEFAULT_LOG_OPS_PATH),
        ok_returncodes=(0,),
    )
    result["kind"] = "heal_lock_skip_probe"
    result["host"] = host
    cutoff = _utc_now() - timedelta(seconds=60)
    matches = [
        line
        for line in result["stdout"].splitlines()
        if line.strip()
        and not line.startswith("==>")
        and "sub-volumes could be locked" in line
        and ":self-heal domain" in line
        and (parsed := _parse_gluster_log_timestamp(line)) is not None
        and parsed >= cutoff
    ]
    result["matches"] = matches
    result["match_count"] = len(matches)
    if result["returncode"] != 0:
        result["ok"] = False
        result["message"] = result["stderr"] or f"unable to inspect glustershd.log on {host}"
        return result
    result["ok"] = True
    if result["match_count"] >= 2:
        result["message"] = (
            f"recent glustershd log on {host} within the last 60 seconds shows repeated self-heal lock skips; "
            f"restart glusterd or the affected brick on {host}, then rerun health-check"
        )
    elif result["match_count"] == 1:
        result["message"] = (
            f"recent glustershd log on {host} within the last 60 seconds shows a self-heal lock skip; "
            f"if the heal rows stay stuck, restart glusterd or the affected brick on {host}"
        )
    else:
        result["message"] = ""
    return result


def _remote_mount_check(
    host: str,
    path: str,
    *,
    ssh_user: str,
    connect_timeout: float,
) -> dict[str, Any]:
    result = _run_remote_check(
        host,
        [
            "findmnt",
            "-T",
            path,
            "-n",
            "-o",
            "TARGET",
        ],
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        use_sudo=False,
    )
    result["kind"] = "brick_mount"
    result["host"] = host
    result["path"] = path
    mountpoint = result["stdout"].strip()
    if result["ok"] and mountpoint:
        result["mountpoint"] = mountpoint
        result["message"] = ""
    else:
        result["message"] = result["stderr"] or f"no mounted ancestor found for {path}"
    return result


def _remote_df_check(
    host: str,
    path: str,
    *,
    ssh_user: str,
    connect_timeout: float,
    mode: str,
) -> dict[str, Any]:
    if mode == "space":
        command = ["df", "-P", "-B1", path]
        label = "brick_free_space"
    else:
        command = ["df", "-Pi", path]
        label = "brick_free_inodes"
    result = _run_remote_check(
        host,
        command,
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        use_sudo=False,
    )
    result["kind"] = label
    result["host"] = host
    result["path"] = path
    result["message"] = ""
    if not result["ok"]:
        result["message"] = result["stderr"] or f"failed to read {label} for {path}"
        return result
    lines = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
    if len(lines) < 2:
        result["ok"] = False
        result["message"] = f"unexpected df output for {path}"
        return result
    payload = lines[-1].split()
    result["payload"] = payload
    try:
        if mode == "space":
            available = int(payload[3])
            result["available_bytes"] = available
            result["ok"] = available > 0
            if not result["ok"]:
                result["message"] = f"no free space reported for {path}"
        else:
            available = int(payload[3])
            result["available_inodes"] = available
            result["ok"] = available > 0
            if not result["ok"]:
                result["message"] = f"no free inodes reported for {path}"
    except (IndexError, ValueError):
        result["ok"] = False
        result["message"] = f"unable to parse df output for {path}"
    return result


def _remote_host_identity_check(
    host: str,
    *,
    ssh_user: str,
    connect_timeout: float,
) -> dict[str, Any]:
    script = (
        "short=$(hostname -s 2>/dev/null || hostname 2>/dev/null || uname -n); "
        "fqdn=$(hostname -f 2>/dev/null || printf '%s' \"$short\"); "
        "ips=$(hostname -I 2>/dev/null || true); "
        "printf 'short=%s\\n' \"$short\"; "
        "printf 'fqdn=%s\\n' \"$fqdn\"; "
        "printf 'ips=%s\\n' \"$ips\""
    )
    result = _run_remote_check(
        host,
        ["sh", "-lc", script],
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        use_sudo=False,
    )
    result["kind"] = "host_identity"
    result["host"] = host
    short_hostname = ""
    fqdn = ""
    ips: list[str] = []
    if result["ok"]:
        for raw_line in result["stdout"].splitlines():
            if "=" not in raw_line:
                continue
            key, value = raw_line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key == "short":
                short_hostname = value
            elif key == "fqdn":
                fqdn = value
            elif key == "ips":
                ips = [item for item in value.split() if item]
    aliases: list[str] = []
    for candidate in (host, short_hostname, fqdn, *ips):
        candidate = candidate.strip()
        if candidate and candidate not in aliases:
            aliases.append(candidate)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        try:
            reverse_name = socket.gethostbyaddr(host)[0]
        except OSError:
            reverse_name = ""
        if reverse_name and reverse_name not in aliases:
            aliases.append(reverse_name)
    result["short_hostname"] = short_hostname
    result["fqdn"] = fqdn
    result["ips"] = ips
    result["aliases"] = aliases
    result["message"] = "" if result["ok"] else result["stderr"] or f"failed to read host identity for {host}"
    return result


def build_volume_health_report(
    volume: str,
    *,
    ssh_user: str = DEFAULT_SERVICE_USER,
    connect_timeout: float = 10.0,
    require_snapshot: bool = False,
    snapshot_ack: bool = False,
    require_self_heal_daemons: bool = False,
) -> dict[str, Any]:
    checked_at = _now_iso()
    report: dict[str, Any] = {
        "schema_version": 1,
        "checked_at": checked_at,
        "volume": volume,
        "ssh_user": ssh_user,
        "connect_timeout": connect_timeout,
        "available": True,
        "error": "",
        "volume_type": "",
        "hosts": [],
        "host_facts": [],
        "bricks": [],
        "self_heal_daemons": [],
        "checks": [],
        "heal_settings": {},
        "reversibility": {
            "snapshot_required": bool(require_snapshot),
            "snapshot_acknowledged": bool(snapshot_ack),
            "snapshot_inventory": {
                "available": False,
                "names": [],
                "count": 0,
                "raw": "",
                "error": "",
            },
        },
        "summary": {
            "checks_checked": 0,
            "checks_ok": 0,
            "checks_failed": 0,
            "hosts_checked": 0,
            "hosts_ok": 0,
            "hosts_failed": 0,
            "bricks_checked": 0,
            "bricks_online": 0,
            "bricks_offline": 0,
            "ready": False,
            "blockers": [],
            "warnings": [],
        },
    }

    blockers: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []
    host_facts: list[dict[str, Any]] = []
    self_heal_daemon_rows: list[dict[str, Any]] = []
    try:
        volume_info = get_volume_info(volume)
        volume_type = parse_volume_type(volume_info)
        report["volume_type"] = volume_type
        if volume_type != "Replicate":
            blockers.append(
                f"unsupported volume type for this tool: {volume!r} is {volume_type or 'unknown'}; only pure Replicate volumes are supported"
            )
        hosts = parse_brick_hosts(volume_info)
        report["hosts"] = hosts
        bricks = parse_bricks(volume_info)
        report["bricks"] = [
            {"host": host, "path": path}
            for host, path in bricks
        ]
    except Exception as exc:
        report["available"] = False
        report["error"] = str(exc)
        report["summary"]["blockers"] = [str(exc)]
        return report

    checks.append(
        {
            "kind": "volume_type",
            "ok": report["volume_type"] == "Replicate",
            "message": "" if report["volume_type"] == "Replicate" else "unsupported volume type",
        }
    )

    heal_daemon_enabled = False
    try:
        heal_settings = get_heal_settings(volume)
        heal_summary = summarize_heal_settings(volume, heal_settings)
        report["heal_settings"] = heal_summary
        checks.append(
            {
                "kind": "heal_settings",
                "ok": True,
                "message": ", ".join(heal_summary.get("warnings") or []),
            }
        )
        warnings.extend(str(message) for message in heal_summary.get("warnings") or [])
        heal_daemon_enabled = str(heal_summary.get("effective_settings", {}).get("cluster.self-heal-daemon") or "").strip().lower() != "off"
    except Exception as exc:
        blockers.append(f"unable to read heal settings: {exc}")
        checks.append(
            {
                "kind": "heal_settings",
                "ok": False,
                "message": str(exc),
            }
        )

    snapshot_available, snapshot_text, snapshot_error = _snapshot_inventory_text(volume)
    snapshot_names = _parse_snapshot_names(snapshot_text) if snapshot_available else []
    report["reversibility"] = {
        "snapshot_required": bool(require_snapshot),
        "snapshot_acknowledged": bool(snapshot_ack),
        "snapshot_inventory": {
            "available": snapshot_available,
            "names": snapshot_names,
            "count": len(snapshot_names),
            "raw": snapshot_text,
            "error": snapshot_error,
        },
    }
    snapshot_message = ""
    if snapshot_available:
        if snapshot_names:
            snapshot_message = f"snapshot inventory available: {len(snapshot_names)} snapshot(s)"
        else:
            snapshot_message = "snapshot inventory available but no snapshots were listed"
    else:
        snapshot_message = snapshot_error or "snapshot inventory unavailable"
    checks.append(
        {
            "kind": "snapshot_inventory",
            "ok": True,
            "available": snapshot_available,
            "count": len(snapshot_names),
            "message": snapshot_message,
            "raw": snapshot_text,
            "error": snapshot_error,
        }
    )
    if require_snapshot and not snapshot_ack:
        blockers.append(
            "snapshot confirmation is required before execute; take a Gluster snapshot or equivalent rollback point, then pass --snapshot-ack"
        )
    try:
        status_text = _volume_status_text(volume)
        status_rows = _parse_volume_status(status_text)
        structured_shd_available = False
        structured_shd_error = ""
        if heal_daemon_enabled and require_self_heal_daemons:
            structured_shd_available, self_heal_daemon_rows, structured_shd_error = _volume_status_shd_xml(volume)
        if not structured_shd_available:
            self_heal_daemon_rows = _parse_self_heal_daemon_status(status_text)
        report["self_heal_daemons"] = self_heal_daemon_rows
        expected = {(host, path) for host, path in bricks}
        seen = {(row["host"], row["path"]) for row in status_rows}
        missing = sorted(expected - seen)
        if missing:
            blockers.append(
                "volume status missing bricks: " + ", ".join(f"{host}:{path}" for host, path in missing)
            )
        for row in status_rows:
            if row["online"]:
                continue
            blockers.append(f"brick reported offline in volume status: {row['host']}:{row['path']}")
        checks.append(
            {
                "kind": "volume_status",
                "ok": not missing and all(row["online"] for row in status_rows if (row["host"], row["path"]) in expected),
                "message": "" if not missing else "missing brick status rows",
                "rows": status_rows,
            }
        )
        report["self_heal_daemon_status_source"] = "xml" if structured_shd_available else "text"
        if structured_shd_error:
            report["self_heal_daemon_status_fallback"] = structured_shd_error
        report["bricks"] = [
            {
                **brick,
                "online": any(row["host"] == brick["host"] and row["path"] == brick["path"] and row["online"] for row in status_rows),
                "pid_running": any(row["host"] == brick["host"] and row["path"] == brick["path"] and row["pid_running"] for row in status_rows),
            }
            for brick in report["bricks"]
        ]
    except Exception as exc:
        blockers.append(str(exc))
        checks.append(
            {
                "kind": "volume_status",
                "ok": False,
                "message": str(exc),
            }
        )

    for host, path in bricks:
        reachable = _run_remote_check(
            host,
            ["true"],
            ssh_user=ssh_user,
            connect_timeout=connect_timeout,
            use_sudo=False,
        )
        reachable.update({"kind": "ssh_reachable", "host": host, "path": path, "message": "" if reachable["ok"] else reachable["stderr"] or "ssh unreachable"})
        checks.append(reachable)
        if not reachable["ok"]:
            blockers.append(f"ssh unreachable for {host}")
            continue

        identity_result = _remote_host_identity_check(
            host,
            ssh_user=ssh_user,
            connect_timeout=connect_timeout,
        )
        checks.append(identity_result)
        aliases = identity_result.get("aliases") or [host]
        host_facts.append(
            {
                "host": host,
                "short_hostname": identity_result.get("short_hostname", ""),
                "fqdn": identity_result.get("fqdn", ""),
                "ips": identity_result.get("ips", []),
                "aliases": aliases,
            }
        )

        for label, remote_path, use_sudo, sudo_path in (
            ("worker_available", str(DEFAULT_WORKER_PATH), False, None),
            ("host_ops_available", str(DEFAULT_HOST_OPS_PATH), False, None),
            ("log_ops_available", str(DEFAULT_LOG_OPS_PATH), False, None),
        ):
            result = _remote_file_check(
                host,
                remote_path,
                ssh_user=ssh_user,
                connect_timeout=connect_timeout,
                label=label,
                use_sudo=use_sudo,
                sudo_path=sudo_path,
            )
            checks.append(result)
            if not result["ok"]:
                blockers.append(f"{label} missing on {host}")

        mount_result = _remote_mount_check(
            host,
            path,
            ssh_user=ssh_user,
            connect_timeout=connect_timeout,
        )
        checks.append(mount_result)
        if not mount_result["ok"]:
            blockers.append(f"brick path has no mounted ancestor on {host}: {path}")

        space_result = _remote_df_check(
            host,
            path,
            ssh_user=ssh_user,
            connect_timeout=connect_timeout,
            mode="space",
        )
        checks.append(space_result)
        if not space_result["ok"]:
            blockers.append(f"no free space reported on {host}:{path}")

        inode_result = _remote_df_check(
            host,
            path,
            ssh_user=ssh_user,
            connect_timeout=connect_timeout,
            mode="inodes",
        )
        checks.append(inode_result)
        if not inode_result["ok"]:
            blockers.append(f"no free inodes reported on {host}:{path}")

    for host in report["hosts"]:
        service_result = _remote_service_status_check(
            host,
            ssh_user=ssh_user,
            connect_timeout=connect_timeout,
            service="glusterd",
        )
        checks.append(service_result)
        if not service_result["ok"]:
            blockers.append(f"glusterd is not active on {host}")

        if heal_daemon_enabled:
            activity_result = _remote_heal_activity_check(
                host,
                volume=volume,
                ssh_user=ssh_user,
                connect_timeout=connect_timeout,
            )
            checks.append(activity_result)
            if activity_result["busy"]:
                message = str(activity_result.get("message") or "gluster heal in progress; wait for `gluster volume heal <vol> statistics heal-count` to settle before rerunning health-check or trying gluster-repair")
                blockers.append(message)
                warnings.append(message)
            elif not activity_result["ok"]:
                warnings.append(str(activity_result.get("message") or f"unable to inspect heal activity on {host}"))

        lock_probe = _remote_heal_lock_skip_probe(
            host,
            volume=volume,
            ssh_user=ssh_user,
            connect_timeout=connect_timeout,
        )
        checks.append(lock_probe)
        lock_message = str(lock_probe.get("message") or "").strip()
        if lock_message:
            warnings.append(lock_message)

    if heal_daemon_enabled and (self_heal_daemon_rows or require_self_heal_daemons):
        shd_check = _self_heal_daemon_check(
            self_heal_daemon_rows,
            bricks=bricks,
            host_facts=host_facts,
            required=require_self_heal_daemons,
        )
        checks.append(shd_check)
        if not shd_check["ok"]:
            blockers.append(str(shd_check["message"]))

    checks_ok = sum(1 for check in checks if check.get("ok"))
    checks_failed = len(checks) - checks_ok
    hosts_checked = len(bricks)
    hosts_ok = sum(1 for host, path in bricks if any(check.get("kind") == "ssh_reachable" and check.get("host") == host and check.get("ok") for check in checks))
    bricks_online = sum(1 for brick in report["bricks"] if brick.get("online"))
    ready = not blockers and checks_failed == 0 and report["volume_type"] == "Replicate"

    report["checks"] = checks
    report["host_facts"] = host_facts
    alias_map = {
        str(item["host"]): item.get("aliases", [])
        for item in host_facts
        if item.get("host")
    }
    if alias_map:
        report["host_aliases"] = alias_map
    report["bricks"] = [
        {
            **brick,
            "aliases": alias_map.get(str(brick.get("host") or ""), []),
        }
        for brick in report["bricks"]
    ]
    report["summary"] = {
        "checks_checked": len(checks),
        "checks_ok": checks_ok,
        "checks_failed": checks_failed,
        "hosts_checked": hosts_checked,
        "hosts_ok": hosts_ok,
        "hosts_failed": hosts_checked - hosts_ok,
        "bricks_checked": len(bricks),
        "bricks_online": bricks_online,
        "bricks_offline": len(bricks) - bricks_online,
        "ready": ready,
        "blockers": blockers,
        "warnings": warnings,
    }
    report["available"] = True
    report["error"] = ""
    return report


def render_volume_health_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    reversibility = report.get("reversibility") or {}
    snapshot_inventory = reversibility.get("snapshot_inventory") or {}
    ready = bool(summary.get("ready"))
    checks_checked = int(summary.get("checks_checked", 0) or 0)
    checks_ok = int(summary.get("checks_ok", 0) or 0)
    hosts_checked = int(summary.get("hosts_checked", 0) or 0)
    hosts_ok = int(summary.get("hosts_ok", 0) or 0)
    bricks_checked = int(summary.get("bricks_checked", 0) or 0)
    bricks_online = int(summary.get("bricks_online", 0) or 0)
    parts = [
        f"Health check: {'ready' if ready else 'blocked'}",
        f"checks {checks_ok}/{checks_checked}",
    ]
    if hosts_checked:
        parts.append(f"hosts {hosts_ok}/{hosts_checked}")
    if bricks_checked:
        parts.append(f"bricks {bricks_online}/{bricks_checked} online")
    if reversibility.get("snapshot_required"):
        parts.append("snapshot gate required")
    if reversibility.get("snapshot_acknowledged"):
        parts.append("snapshot acknowledged")
    if snapshot_inventory:
        snapshot_count = int(snapshot_inventory.get("count", 0) or 0)
        if snapshot_inventory.get("available"):
            parts.append(f"snapshots {snapshot_count}")
        else:
            parts.append("snapshot inventory unavailable")
    blockers = summary.get("blockers") or []
    if blockers:
        parts.append(f"blockers={len(blockers)}")
    warnings = summary.get("warnings") or []
    if warnings:
        parts.append(f"warnings={len(warnings)}")
    error = str(report.get("error") or "").strip()
    if error:
        parts.append(f"error: {error}")
    return "; ".join(parts)


def write_health_report(path: str | Path, report: dict[str, Any]) -> None:
    write_json_shared(path, report)


def build_brick_layout_cache(report: dict[str, Any]) -> dict[str, Any]:
    layout: dict[str, Any] = {
        "schema_version": report.get("schema_version", 1),
        "source": "health-check",
        "checked_at": report.get("checked_at", ""),
        "volume": report.get("volume", ""),
        "volume_type": report.get("volume_type", ""),
        "hosts": list(report.get("hosts") or []),
        "bricks": list(report.get("bricks") or []),
        "host_facts": list(report.get("host_facts") or []),
    }
    host_aliases = report.get("host_aliases") or {}
    if isinstance(host_aliases, dict) and host_aliases:
        layout["host_aliases"] = {
            str(host): [str(alias).strip() for alias in aliases or [] if str(alias).strip()]
            for host, aliases in host_aliases.items()
            if str(host).strip()
        }
    return layout


def write_brick_layout_cache(path: str | Path, report: dict[str, Any]) -> dict[str, Any]:
    layout = build_brick_layout_cache(report)
    write_json_shared(path, layout)
    return layout


def health_check_warnings(status: dict[str, Any], *, max_age_seconds: int = HEALTH_REPORT_STALE_SECONDS) -> list[str]:
    warnings: list[str] = []
    health = status.get("health_check") or {}
    if not isinstance(health, dict) or not health:
        warnings.append("no health-check report recorded; run health-check first")
        return warnings
    summary = health.get("summary") or {}
    if not summary.get("ready"):
        warnings.append("last health-check report is not ready")
    checked_at = str(health.get("checked_at") or "").strip()
    if not checked_at:
        warnings.append("health-check report has no timestamp")
    else:
        try:
            checked = datetime.fromisoformat(checked_at)
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds()
            if age > max_age_seconds:
                warnings.append(
                    f"health-check report is stale by {int(age)} seconds; rerun health-check before executing"
                )
        except ValueError:
            warnings.append("health-check report timestamp is invalid")
    status_volume = str(status.get("volume") or "").strip()
    health_volume = str(health.get("volume") or "").strip()
    if status_volume and health_volume and status_volume != health_volume:
        warnings.append(
            f"health-check volume {health_volume} does not match current status volume {status_volume}"
        )
    status_hosts = [str(item).strip() for item in status.get("hosts") or [] if str(item).strip()]
    health_hosts = [str(item).strip() for item in health.get("hosts") or [] if str(item).strip()]
    if status_hosts and health_hosts and sorted(status_hosts) != sorted(health_hosts):
        warnings.append("health-check hosts do not match the current status hosts")
    status_brick_path = str(status.get("brick_path") or "").strip()
    health_bricks = health.get("bricks") or []
    health_brick_paths = sorted(
        {str(item.get("path") or "").strip() for item in health_bricks if isinstance(item, dict) and str(item.get("path") or "").strip()}
    )
    if status_brick_path and health_brick_paths and status_brick_path not in health_brick_paths:
        warnings.append("health-check brick path does not match the current status brick path")
    return warnings
