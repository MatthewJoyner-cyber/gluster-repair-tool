# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Manifest and resolution manager helpers."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .controller_paths import default_brick_layout_path
from .controller_paths import default_heal_info_root
from .controller_paths import default_health_report_path
from .install_paths import DEFAULT_SERVICE_USER
from .health import build_volume_health_report, render_volume_health_summary, write_brick_layout_cache, write_health_report
from .heal_parser import parse_heal_info_file, parse_heal_info_text
from .shared_io import write_text_shared
from .manifest import build_manifest, write_manifest
from .models import HealEntry, ResolutionObservation
from .protocol import ResolveBatchRequest, observations_from_payload
from .resolver import CachedResolver, dump_observations
from .ssh_identity import ssh_identity_options
from .status import update_status
from .temp_mount import inspect_mount_at_path, inspect_mount_at_path_lexically
from .volume import discover_brick_hosts, discover_brick_paths, discover_common_brick_path, get_heal_info_text, get_volume_info, parse_brick_roles
from .volume import normalize_host_alias
from .volume import run_heal


class ProgressLog:
    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%F %T')}] {message}"
        print(line, file=sys.stderr)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def _unique_raw_entries(heal_entries) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in heal_entries:
        if entry.raw in seen:
            continue
        seen.add(entry.raw)
        ordered.append(entry.raw)
    return ordered


def _live_split_brain_gfids(volume: str) -> tuple[set[str], str]:
    """Return current split-brain GFIDs without starting a heal crawl."""
    try:
        entries = parse_heal_info_text(get_heal_info_text(volume))
    except RuntimeError as exc:
        return set(), str(exc)
    gfids = {
        entry.parent_gfid
        for entry in entries
        if entry.split_brain and entry.parent_gfid
    }
    if any(entry.split_brain and not entry.parent_gfid for entry in entries):
        return gfids, "split-brain heal row lacks a GFID identity"
    return gfids, ""


def _annotate_live_split_brain(manifest, split_brain_gfids: set[str], error: str = "") -> None:
    for obj in manifest.values():
        # An index-entry seed names its GFID in the final path component.
        index_gfids = {
            Path(raw).name for raw in obj.raw_entries
            if "/.glusterfs/indices/" in raw
        }
        object_gfids = {value.lower() for value in (
            set(obj.gfids) | set(obj.file_gfids) | set(obj.dead_gfids) | index_gfids
        )}
        if object_gfids & {value.lower() for value in split_brain_gfids} and "heal_info_marks_split_brain" not in obj.notes:
            obj.notes.append("heal_info_marks_split_brain")
        if error:
            obj.notes.append("live_split_brain_evidence_unavailable")
        elif "live_split_brain_checked" not in obj.notes:
            obj.notes.append("live_split_brain_checked")



def _load_brick_role_evidence(volume: str) -> tuple[dict[str, str], str]:
    try:
        return parse_brick_roles(get_volume_info(volume)), ""
    except RuntimeError as exc:
        return {}, str(exc)

def _run_resolve_worker(
    *,
    host: str,
    request: ResolveBatchRequest,
    worker_path: str,
    ssh_user: str,
    local_aliases: set[str],
) -> list[ResolutionObservation]:
    if normalize_host_alias(host) in local_aliases:
        local_cmd = [worker_path, "resolve-batch"]
        if os.geteuid() != 0:
            local_cmd = ["sudo", "-n", *local_cmd]
        proc = subprocess.run(
            local_cmd,
            input=json.dumps(request.to_dict()),
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        remote_cmd = _remote_command([worker_path, "resolve-batch"], use_sudo=ssh_user != "root")
        proc = subprocess.run(
            [
                "ssh",
                *ssh_identity_options(),
                "-x",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                f"{ssh_user}@{host}",
                remote_cmd,
            ],
            input=json.dumps(request.to_dict()),
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        message = proc.stderr.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"worker failed on {host}: {message}")
    payload = json.loads(proc.stdout)
    return observations_from_payload(payload)

def _local_host_aliases() -> set[str]:
    aliases = {
        normalize_host_alias(socket.gethostname()),
        normalize_host_alias(socket.getfqdn()),
        normalize_host_alias(os.uname().nodename),
    }
    return {alias for alias in aliases if alias}


def _parse_gluster_mount_source(source: str) -> tuple[str, str, str]:
    value = str(source or "").strip()
    if ":" not in value:
        raise RuntimeError(f"path repair requires a Gluster mount source, got {value or 'unknown source'}")
    host, _, suffix = value.partition(":")
    host = host.strip()
    suffix = suffix.strip().lstrip("/")
    if not host or not suffix:
        raise RuntimeError(f"path repair requires a Gluster mount source, got {value or 'unknown source'}")
    volume, _, tail = suffix.partition("/")
    if not volume:
        raise RuntimeError(f"unable to parse Gluster volume from mount source {value!r}")
    return host, volume, tail


def _path_relative_to_mount(path: str, mountpoint: str, *, resolve_path: bool) -> str:
    if resolve_path:
        requested = Path(path).expanduser().resolve(strict=False)
        mount_root = Path(mountpoint).expanduser()
    else:
        requested = Path(os.path.normpath(os.path.abspath(os.path.expanduser(path))))
        mount_root = Path(os.path.normpath(os.path.abspath(os.path.expanduser(mountpoint))))
    try:
        relative = requested.relative_to(mount_root)
    except ValueError as exc:
        raise RuntimeError(
            f"path {requested} is not under the detected Gluster mountpoint {mount_root}"
        ) from exc
    logical = relative.as_posix().strip("/")
    if not logical:
        raise RuntimeError(
            f"path {requested} resolves to the mount root {mount_root}; point repair-meta at a file or subdirectory beneath the mount"
        )
    return logical


def _validate_path_mount_context(path: str, *, probe_mount: bool) -> dict[str, str]:
    if probe_mount:
        requested = str(Path(path).expanduser().resolve(strict=False))
        info = inspect_mount_at_path(requested)
    else:
        requested = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
        info = inspect_mount_at_path_lexically(requested)
    if not info:
        raise RuntimeError(f"unable to inspect a mount for path {requested}")
    source = str(info.get("source") or "").strip()
    fstype = str(info.get("fstype") or "").strip()
    target = str(info.get("target") or "").strip()
    options = str(info.get("options") or "").strip()
    option_bits = {item.strip().lower() for item in options.split(",") if item.strip()}
    if "bind" in option_bits or "rbind" in option_bits:
        raise RuntimeError(
            f"path repair does not support bind mounts yet; use the underlying Gluster mount directly for {requested}"
        )
    if "subdir" in options.lower():
        raise RuntimeError(
            f"path repair does not yet support Gluster subdirectory mounts; use a direct mount for {requested}"
        )
    if "gluster" not in fstype.lower():
        raise RuntimeError(
            f"path repair expected a Gluster filesystem for {requested}, found {fstype or 'unknown filesystem'}"
        )
    source_host, volume, source_tail = _parse_gluster_mount_source(source)
    if source_tail:
        raise RuntimeError(
            f"path repair does not yet support mounted subdirectories; source {source!r} resolves to volume {volume!r} with extra path {source_tail!r}"
        )
    logical_path = _path_relative_to_mount(requested, target, resolve_path=probe_mount)
    return {
        "requested_path": requested,
        "source": source,
        "fstype": fstype,
        "mountpoint": target,
        "options": options,
        "source_host": source_host,
        "volume": volume,
        "logical_path": logical_path,
        "raw_entry": f"/{logical_path}",
    }


def _validate_backend_path_context(volume: str, backend_path: str, mountpoint: str | None) -> dict[str, str]:
    requested = Path(backend_path).expanduser().resolve(strict=False)
    brick_paths = discover_brick_paths(volume)
    matches: list[tuple[str, str, str]] = []
    for host, brick_root in sorted(brick_paths.items()):
        resolved_root = Path(brick_root).expanduser().resolve(strict=False)
        try:
            relative = requested.relative_to(resolved_root)
        except ValueError:
            continue
        logical_path = relative.as_posix().strip("/")
        if not logical_path:
            raise RuntimeError(
                f"backend path {requested} resolves to brick root {resolved_root}; point repair-meta at an object beneath the brick root"
            )
        matches.append((host, str(resolved_root), logical_path))
    if not matches:
        known_roots = ", ".join(sorted(set(brick_paths.values()))) or "(none)"
        raise RuntimeError(
            f"backend path {requested} is not beneath a recorded brick root for volume {volume}; known roots: {known_roots}"
        )
    logical_paths = {item[2] for item in matches}
    if len(logical_paths) != 1:
        raise RuntimeError(
            f"backend path {requested} maps ambiguously within volume {volume}: {', '.join(sorted(logical_paths))}"
        )
    source_host, source_root, logical_path = matches[0]
    display_mountpoint = str(Path(mountpoint or f"/{volume}").expanduser())
    return {
        "requested_backend_path": str(requested),
        "volume": volume,
        "mountpoint": display_mountpoint,
        "logical_path": logical_path,
        "raw_entry": f"/{logical_path}",
        "source_host": "localhost",
        "matched_backend_host": source_host,
        "matched_backend_root": source_root,
        "source": f"operator-backend:{requested}",
    }


def _normalize_gfid_value(value: str) -> str:
    cleaned = str(value or '').strip()
    if not cleaned:
        raise RuntimeError('repair-meta --gfid requires a GFID value')
    if cleaned.startswith('<gfid:') and cleaned.endswith('>') and '/' not in cleaned:
        cleaned = cleaned[6:-1]
    try:
        return str(uuid.UUID(cleaned))
    except (ValueError, AttributeError, TypeError):
        try:
            return str(uuid.UUID(hex=cleaned.replace('-', '')))
        except (ValueError, AttributeError, TypeError) as exc:
            raise RuntimeError(f'invalid GFID value {value!r}') from exc


def _normalize_gfid_child_entry(value: str) -> tuple[str, str]:
    cleaned = str(value or '').strip()
    if not cleaned:
        raise RuntimeError('repair-meta --gfid-child requires UUID/child or <gfid:UUID>/child')
    if cleaned.startswith('<gfid:'):
        if '>' not in cleaned:
            raise RuntimeError('repair-meta --gfid-child requires UUID/child or <gfid:UUID>/child')
        prefix, child_rel = cleaned.split('>', 1)
        canonical = _normalize_gfid_value(prefix[6:])
    else:
        raw_gfid, separator, child_rel = cleaned.partition('/')
        if not separator:
            raise RuntimeError('repair-meta --gfid-child requires UUID/child or <gfid:UUID>/child')
        canonical = _normalize_gfid_value(raw_gfid)
    child_rel = child_rel.lstrip('/')
    if not child_rel:
        raise RuntimeError('repair-meta --gfid-child requires a child path after the GFID')
    return canonical, child_rel


def _normalize_index_entry(value: str) -> str:
    cleaned = str(value or '').strip()
    if not cleaned:
        raise RuntimeError('repair-meta --index-entry requires a heal entry path')
    if not cleaned.startswith('/'):
        cleaned = f'/{cleaned.lstrip("/")}'
    logical = cleaned.lstrip('/')
    parts = logical.split('/', 3)
    if len(parts) < 4 or parts[0] != '.glusterfs' or parts[1] != 'indices' or parts[2] not in {'xattrop', 'dirty'} or not parts[3]:
        raise RuntimeError(
            'repair-meta --index-entry must point at .glusterfs/indices/xattrop/<gfid> or .glusterfs/indices/dirty/<gfid>'
        )
    return cleaned


def _resolve_via_operator_entry(
    *,
    volume: str,
    raw_entry: str,
    mountpoint: str | None,
    resolver_path: str,
    worker_path: str,
    manifest_out: str,
    observations_out: str,
    source_host: str,
    source: str,
    evidence_route: str,
    input_kind: str,
    logical_path: str | None = None,
    input_source: str = "operator_path",
    ssh_user: str = DEFAULT_SERVICE_USER,
    log_path: str | None = None,
    checkpoint_every_host: bool = True,
    verbose: bool = False,
    probe_mount: bool = False,
    related_raw_entries: list[str] | None = None,
    brick_host_aliases: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    progress = ProgressLog(log_path)
    entry = str(raw_entry).strip()
    if not entry:
        raise RuntimeError('missing repair-meta entry')
    display_mountpoint = str(Path(mountpoint or f'/{volume}').expanduser())
    progress.log(
        f'{input_kind} repair context resolved entry={entry} volume={volume} mountpoint={display_mountpoint}'
    )
    requested_entries = list(dict.fromkeys([entry, *(related_raw_entries or [])]))
    heal_entries = [
        HealEntry(
            raw=requested_entry,
            source_host=source_host,
            brick=source,
            index_on_host=0,
            input_source=input_source,
        )
        for requested_entry in requested_entries
    ]
    hosts = discover_brick_hosts(volume)
    try:
        brick_paths = discover_brick_paths(volume)
    except RuntimeError:
        brick_paths = {}
    unique_paths = {value for value in brick_paths.values()}
    brick_path = next(iter(unique_paths)) if len(unique_paths) == 1 else ''
    split_brain_gfids, split_brain_evidence_error = _live_split_brain_gfids(volume)
    summary = _resolve_entries_via_workers(
        heal_entries=heal_entries,
        volume=volume,
        hosts=hosts,
        brick_path=brick_path or None,
        brick_paths=brick_paths or None,
        mountpoint=display_mountpoint,
        resolver_path=resolver_path,
        worker_path=worker_path,
        manifest_out=manifest_out,
        observations_out=observations_out,
        ssh_user=ssh_user,
        log_path=log_path,
        checkpoint_every_host=checkpoint_every_host,
        verbose=verbose,
        brick_role_evidence_required=True,
        brick_host_aliases=brick_host_aliases,
        probe_mount=probe_mount,
        split_brain_gfids=split_brain_gfids,
        split_brain_evidence_error=split_brain_evidence_error,
    )
    summary.update(
        {
            'path': entry,
            'volume': volume,
            'mountpoint': display_mountpoint,
            'logical_path': logical_path or entry.lstrip('/'),
            'source': source,
            'source_host': source_host,
            'brick_path': brick_path,
            'evidence_route': evidence_route,
            'mount_probe': probe_mount,
            'input_source': input_source,
            'requested_seed': entry,
            'repair_meta_input': entry,
            'repair_meta_input_kind': input_kind,
            'live_split_brain_gfid_count': len(split_brain_gfids),
            'live_split_brain_evidence_error': split_brain_evidence_error,
        }
    )
    return summary


def _remote_command(parts: list[str], *, use_sudo: bool) -> str:
    command = ["sudo", "-n", *parts] if use_sudo else list(parts)
    return subprocess.list2cmdline(command)


def _heal_snapshot_root(heal_root: str | None = None) -> Path:
    if heal_root:
        return Path(heal_root).expanduser()
    return default_heal_info_root()


def _heal_snapshot_dir(volume: str, heal_root: str | None = None) -> Path:
    safe_volume = volume.strip().strip("/") or "volume"
    return _heal_snapshot_root(heal_root) / safe_volume


def _default_heal_snapshot_path(volume: str, heal_root: str | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_volume = volume.strip().strip("/") or "volume"
    return _heal_snapshot_dir(volume, heal_root) / f"{safe_volume}-heal-info-{stamp}.txt"


def _latest_heal_snapshot_path(volume: str, heal_root: str | None = None) -> Path | None:
    snapshot_dir = _heal_snapshot_dir(volume, heal_root)
    if not snapshot_dir.exists():
        return None
    safe_volume = volume.strip().strip("/") or "volume"
    candidates = sorted(
        snapshot_dir.glob(f"{safe_volume}-heal-info-*.txt"),
        key=lambda path: (
            path.stat().st_mtime_ns if path.exists() else 0,
            path.name,
        ),
    )
    return candidates[-1] if candidates else None


HEAL_REFRESH_ERROR_PREFIX = "# gluster-repair heal refresh error: "

def _heal_refresh_error(path: str | Path) -> str:
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.startswith(HEAL_REFRESH_ERROR_PREFIX):
                return line.removeprefix(HEAL_REFRESH_ERROR_PREFIX).strip()
    except OSError:
        return ""
    return ""


def capture_live_heal_snapshot(
    volume: str,
    *,
    heal_out: str | None = None,
    heal_root: str | None = None,
    heal_refresh: bool = True,
) -> str:
    target = Path(heal_out).expanduser() if heal_out else _default_heal_snapshot_path(volume, heal_root)
    refresh_error = ""
    if heal_refresh:
        try:
            run_heal(volume)
        except RuntimeError as exc:
            refresh_error = " ".join(str(exc).split())
    heal_text = get_heal_info_text(volume)
    if refresh_error:
        heal_text = f"{HEAL_REFRESH_ERROR_PREFIX}{refresh_error}\n{heal_text}"
    write_text_shared(target, heal_text)
    return str(target)


def resolve_heal_snapshot(
    volume: str,
    *,
    heal_file: str | None = None,
    heal_latest: bool = False,
    heal_fresh: bool = False,
    heal_out: str | None = None,
    heal_root: str | None = None,
    heal_refresh: bool = True,
) -> str:
    if heal_fresh:
        return capture_live_heal_snapshot(volume, heal_out=heal_out, heal_root=heal_root, heal_refresh=heal_refresh)
    if heal_file and heal_file.strip():
        value = heal_file.strip()
        if value.lower() != "latest":
            return value
        heal_latest = True
    if heal_latest:
        latest = _latest_heal_snapshot_path(volume, heal_root)
        if latest is not None:
            return str(latest)
    return capture_live_heal_snapshot(volume, heal_out=heal_out, heal_root=heal_root, heal_refresh=heal_refresh)


def _resolve_entries_via_workers(
    *,
    heal_entries,
    volume: str,
    hosts: list[str],
    brick_path: str | None,
    brick_paths: dict[str, str] | None = None,
    mountpoint: str,
    resolver_path: str,
    worker_path: str,
    manifest_out: str,
    observations_out: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    log_path: str | None = None,
    checkpoint_every_host: bool = True,
    verbose: bool = False,
    probe_mount: bool = True,
    brick_roles_by_host: dict[str, str] | None = None,
    brick_host_aliases: dict[str, list[str]] | None = None,
    brick_role_evidence_required: bool = False,
    brick_role_evidence_error: str = "",
    split_brain_gfids: set[str] | None = None,
    split_brain_evidence_error: str = "",
) -> dict[str, object]:
    volume_id = ""
    if brick_role_evidence_required and brick_roles_by_host is None:
        try:
            from .apply_binding import volume_identity, BindingError
            volume_info = get_volume_info(volume)
            brick_roles_by_host = parse_brick_roles(volume_info)
            volume_id = volume_identity(volume_info)
        except (RuntimeError, BindingError) as exc:
            brick_role_evidence_error = str(exc)
    progress = ProgressLog(log_path)
    unique_entries = _unique_raw_entries(heal_entries)
    progress.log(
        f"loaded heal entries: {len(heal_entries)} raw entries, {len(unique_entries)} unique raw entries"
    )

    host_jobs = []
    local_aliases = _local_host_aliases()
    for idx, host in enumerate(hosts, start=1):
        host_brick_path = brick_path
        if brick_paths is not None:
            host_brick_path = brick_paths.get(normalize_host_alias(host), host_brick_path)
        if not host_brick_path:
            raise RuntimeError(f"no brick path available for host {host} in volume {volume}")
        request = ResolveBatchRequest(
            volume=volume,
            brick_path=host_brick_path,
            mountpoint=mountpoint,
            resolver_path=resolver_path,
            entries=unique_entries,
            verbose=verbose,
            probe_mount=probe_mount,
        )
        progress.log(f"queueing host {host} ({idx}/{len(hosts)}) with {len(unique_entries)} entries")
        host_jobs.append((idx, host, request))

    observations_by_index = {}
    max_workers = max(1, len(host_jobs))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_job = {
            executor.submit(
                _run_resolve_worker,
                host=host,
                request=request,
                worker_path=worker_path,
                ssh_user=ssh_user,
                local_aliases=local_aliases,
            ): (idx, host)
            for idx, host, request in host_jobs
        }
        for future in as_completed(future_to_job):
            idx, host = future_to_job[future]
            host_observations = future.result()
            # One machine may serve multiple brick endpoints; the queried endpoint is the brick identity.
            for observation in host_observations:
                observation.host = host
            observations_by_index[idx] = host_observations
            error_count = sum(1 for item in host_observations if item.error)
            progress.log(
                f"host {host} resolved {len(host_observations)} entries; errors={error_count}"
            )
            if checkpoint_every_host:
                checkpoint_observations = []
                for completed_idx in sorted(observations_by_index):
                    checkpoint_observations.extend(observations_by_index[completed_idx])
                dump_observations(observations_out, checkpoint_observations)
                progress.log(f"checkpointed observations after host {host} -> {observations_out}")

    all_observations = []
    for idx, _host, _request in host_jobs:
        all_observations.extend(observations_by_index[idx])

    resolver = CachedResolver(all_observations)
    manifest, observations = build_manifest(
        heal_entries,
        resolver,
        brick_roles_by_host=brick_roles_by_host,
        brick_host_aliases=brick_host_aliases,
        brick_role_evidence_required=brick_role_evidence_required,
        brick_role_evidence_error=brick_role_evidence_error,
    )
    if split_brain_gfids is not None or split_brain_evidence_error:
        _annotate_live_split_brain(manifest, split_brain_gfids or set(), split_brain_evidence_error)
    write_manifest(
        manifest_out, manifest, volume=volume, volume_id=volume_id,
        bricks=[{"host": host, "path": request.brick_path,
                 "role": (brick_roles_by_host or {}).get(host, "")}
                for _idx, host, request in host_jobs],
    )
    dump_observations(observations_out, observations)

    summary = {
        "raw_entries": len(heal_entries),
        "unique_raw_entries": len(unique_entries),
        "observations": len(observations),
        "object_types": Counter(obj.object_type for obj in manifest.values()),
        "input_source": heal_entries[0].input_source if heal_entries else "heal_info",
        "requested_seed": unique_entries[0] if unique_entries else "",
    }
    progress.log(f"manifest complete -> {manifest_out}")
    progress.log(f"observations complete -> {observations_out}")
    progress.log(f"summary: {summary}")
    return summary


def resolve_via_workers(
    *,
    heal_file: str | None,
    volume: str,
    hosts: list[str],
    brick_path: str | None,
    brick_paths: dict[str, str] | None = None,
    mountpoint: str,
    resolver_path: str,
    worker_path: str,
    manifest_out: str,
    observations_out: str,
    heal_out: str | None = None,
    heal_root: str | None = None,
    heal_latest: bool = False,
    heal_fresh: bool = False,
    ssh_user: str = DEFAULT_SERVICE_USER,
    log_path: str | None = None,
    checkpoint_every_host: bool = True,
    verbose: bool = False,
    probe_mount: bool = True,
    brick_host_aliases: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    heal_file = resolve_heal_snapshot(
        volume,
        heal_file=heal_file,
        heal_latest=heal_latest,
        heal_fresh=heal_fresh,
        heal_out=heal_out,
        heal_root=heal_root,
    )
    heal_entries = parse_heal_info_file(heal_file)
    summary = _resolve_entries_via_workers(
        heal_entries=heal_entries,
        volume=volume,
        hosts=hosts,
        brick_path=brick_path,
        brick_paths=brick_paths,
        mountpoint=mountpoint,
        resolver_path=resolver_path,
        worker_path=worker_path,
        manifest_out=manifest_out,
        observations_out=observations_out,
        ssh_user=ssh_user,
        log_path=log_path,
        checkpoint_every_host=checkpoint_every_host,
        verbose=verbose,
        brick_role_evidence_required=True,
        brick_host_aliases=brick_host_aliases,
        probe_mount=probe_mount,
    )
    summary["heal_file"] = heal_file
    refresh_error = _heal_refresh_error(heal_file)
    summary["heal_refresh_error"] = refresh_error
    summary["heal_snapshot_fresh"] = not bool(refresh_error)
    return summary


def resolve_via_volume(
    *,
    heal_file: str | None,
    volume: str,
    brick_path: str | None,
    brick_paths: dict[str, str] | None = None,
    mountpoint: str,
    resolver_path: str,
    worker_path: str,
    manifest_out: str,
    observations_out: str,
    heal_out: str | None = None,
    heal_root: str | None = None,
    heal_latest: bool = False,
    heal_fresh: bool = False,
    ssh_user: str = DEFAULT_SERVICE_USER,
    log_path: str | None = None,
    verbose: bool = False,
    probe_mount: bool = True,
    brick_host_aliases: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    hosts = discover_brick_hosts(volume)
    if brick_paths is None:
        try:
            brick_paths = discover_brick_paths(volume)
        except RuntimeError:
            brick_paths = {}
    if not brick_path and brick_paths:
        unique_paths = {path for path in brick_paths.values()}
        if len(unique_paths) == 1:
            brick_path = next(iter(unique_paths))
    return resolve_via_workers(
        heal_file=heal_file,
        volume=volume,
        hosts=hosts,
        brick_path=brick_path,
        brick_paths=brick_paths,
        mountpoint=mountpoint,
        resolver_path=resolver_path,
        worker_path=worker_path,
        ssh_user=ssh_user,
        manifest_out=manifest_out,
        observations_out=observations_out,
        heal_out=heal_out,
        heal_root=heal_root,
        heal_latest=heal_latest,
        heal_fresh=heal_fresh,
        log_path=log_path,
        verbose=verbose,
        brick_host_aliases=brick_host_aliases,
        probe_mount=probe_mount,
    )


def resolve_via_path(
    *,
    path: str,
    resolver_path: str,
    worker_path: str,
    manifest_out: str,
    observations_out: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    log_path: str | None = None,
    checkpoint_every_host: bool = True,
    verbose: bool = False,
    probe_mount: bool = True,
    brick_host_aliases: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    progress = ProgressLog(log_path)
    context = _validate_path_mount_context(path, probe_mount=probe_mount)
    volume = context["volume"]
    mountpoint = context["mountpoint"]
    logical_path = context["logical_path"]
    raw_entry = context["raw_entry"]
    source_host = context["source_host"]
    source = context["source"]
    fstype = context["fstype"]
    options = context["options"]
    split_brain_gfids, split_brain_evidence_error = _live_split_brain_gfids(volume)
    progress.log(
        f"path repair context resolved path={context['requested_path']} mountpoint={mountpoint} volume={volume} logical={logical_path}"
    )
    heal_entries = [
        HealEntry(
            raw=raw_entry,
            source_host=source_host,
            brick=source,
            index_on_host=0,
            input_source="operator_path",
        )
    ]
    hosts = discover_brick_hosts(volume)
    try:
        brick_paths = discover_brick_paths(volume)
    except RuntimeError:
        brick_paths = {}
    brick_path = ""
    unique_paths = {value for value in brick_paths.values()}
    if len(unique_paths) == 1:
        brick_path = next(iter(unique_paths))
    summary = _resolve_entries_via_workers(
        heal_entries=heal_entries,
        volume=volume,
        hosts=hosts,
        brick_path=brick_path or None,
        brick_paths=brick_paths or None,
        mountpoint=mountpoint,
        resolver_path=resolver_path,
        worker_path=worker_path,
        manifest_out=manifest_out,
        observations_out=observations_out,
        ssh_user=ssh_user,
        log_path=log_path,
        checkpoint_every_host=checkpoint_every_host,
        verbose=verbose,
        brick_role_evidence_required=True,
        brick_host_aliases=brick_host_aliases,
        probe_mount=probe_mount,
        split_brain_gfids=split_brain_gfids,
        split_brain_evidence_error=split_brain_evidence_error,
    )
    summary.update(
        {
            "path": context["requested_path"],
            "volume": volume,
            "mountpoint": mountpoint,
            "logical_path": logical_path,
            "source": source,
            "source_host": source_host,
            "fstype": fstype,
            "options": options,
            "brick_path": brick_path,
            "input_source": "operator_path",
            "requested_seed": context["requested_path"],
            "live_split_brain_gfid_count": len(split_brain_gfids),
            "live_split_brain_evidence_error": split_brain_evidence_error,
        }
    )
    return summary


def resolve_via_backend_path(
    *,
    volume: str,
    backend_path: str,
    mountpoint: str | None,
    resolver_path: str,
    worker_path: str,
    manifest_out: str,
    observations_out: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    log_path: str | None = None,
    checkpoint_every_host: bool = True,
    verbose: bool = False,
    brick_host_aliases: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    """Resolve operator-supplied backend evidence without probing a client mount."""
    progress = ProgressLog(log_path)
    context = _validate_backend_path_context(volume, backend_path, mountpoint)
    progress.log(
        "backend repair context resolved "
        f"path={context['requested_backend_path']} volume={volume} logical={context['logical_path']} "
        f"brick_root={context['matched_backend_root']}"
    )
    heal_entries = [
        HealEntry(
            raw=context["raw_entry"],
            source_host=context["source_host"],
            brick=context["source"],
            index_on_host=0,
            input_source="operator_path",
        )
    ]
    hosts = discover_brick_hosts(volume)
    brick_paths = discover_brick_paths(volume)
    unique_paths = {value for value in brick_paths.values()}
    brick_path = next(iter(unique_paths)) if len(unique_paths) == 1 else ""
    split_brain_gfids, split_brain_evidence_error = _live_split_brain_gfids(volume)
    summary = _resolve_entries_via_workers(
        heal_entries=heal_entries,
        volume=volume,
        hosts=hosts,
        brick_path=brick_path or None,
        brick_paths=brick_paths or None,
        mountpoint=context["mountpoint"],
        resolver_path=resolver_path,
        worker_path=worker_path,
        manifest_out=manifest_out,
        observations_out=observations_out,
        ssh_user=ssh_user,
        log_path=log_path,
        checkpoint_every_host=checkpoint_every_host,
        verbose=verbose,
        brick_role_evidence_required=True,
        brick_host_aliases=brick_host_aliases,
        probe_mount=False,
        split_brain_gfids=split_brain_gfids,
        split_brain_evidence_error=split_brain_evidence_error,
    )
    summary.update(
        {
            "path": context["requested_backend_path"],
            "backend_path": context["requested_backend_path"],
            "volume": volume,
            "mountpoint": context["mountpoint"],
            "logical_path": context["logical_path"],
            "source": context["source"],
            "source_host": context["source_host"],
            "brick_path": brick_path,
            "evidence_route": "backend-path",
            "mount_probe": False,
            "input_source": "operator_path",
            "requested_seed": context["requested_backend_path"],
            "live_split_brain_gfid_count": len(split_brain_gfids),
            "live_split_brain_evidence_error": split_brain_evidence_error,
        }
    )
    return summary


def resolve_via_gfid(
    *,
    volume: str,
    gfid: str,
    mountpoint: str | None,
    resolver_path: str,
    worker_path: str,
    manifest_out: str,
    observations_out: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    log_path: str | None = None,
    checkpoint_every_host: bool = True,
    verbose: bool = False,
    brick_host_aliases: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    canonical = _normalize_gfid_value(gfid)
    raw_entry = f'<gfid:{canonical}>'
    return _resolve_via_operator_entry(
        volume=volume,
        raw_entry=raw_entry,
        mountpoint=mountpoint,
        resolver_path=resolver_path,
        worker_path=worker_path,
        manifest_out=manifest_out,
        observations_out=observations_out,
        source_host='localhost',
        source=f'operator-gfid:{canonical}',
        evidence_route='gfid',
        input_kind='gfid',
        logical_path=raw_entry,
        input_source='operator_gfid',
        ssh_user=ssh_user,
        log_path=log_path,
        checkpoint_every_host=checkpoint_every_host,
        verbose=verbose,
        brick_host_aliases=brick_host_aliases,
    )


def resolve_via_gfid_child(
    *,
    volume: str,
    gfid_child: str,
    mountpoint: str | None,
    resolver_path: str,
    worker_path: str,
    manifest_out: str,
    observations_out: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    log_path: str | None = None,
    checkpoint_every_host: bool = True,
    verbose: bool = False,
    brick_host_aliases: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    canonical, child_rel = _normalize_gfid_child_entry(gfid_child)
    raw_entry = f'<gfid:{canonical}>/{child_rel}'
    return _resolve_via_operator_entry(
        volume=volume,
        raw_entry=raw_entry,
        mountpoint=mountpoint,
        resolver_path=resolver_path,
        worker_path=worker_path,
        manifest_out=manifest_out,
        observations_out=observations_out,
        source_host='localhost',
        source=f'operator-gfid-child:{canonical}/{child_rel}',
        evidence_route='gfid-child',
        input_kind='gfid-child',
        logical_path=raw_entry,
        input_source='operator_gfid_child',
        related_raw_entries=[f'<gfid:{canonical}>'],
        ssh_user=ssh_user,
        log_path=log_path,
        checkpoint_every_host=checkpoint_every_host,
        verbose=verbose,
        brick_host_aliases=brick_host_aliases,
    )


def resolve_via_index_entry(
    *,
    volume: str,
    index_entry: str,
    mountpoint: str | None,
    resolver_path: str,
    worker_path: str,
    manifest_out: str,
    observations_out: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    log_path: str | None = None,
    checkpoint_every_host: bool = True,
    verbose: bool = False,
    brick_host_aliases: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    normalized_entry = _normalize_index_entry(index_entry)
    return _resolve_via_operator_entry(
        volume=volume,
        raw_entry=normalized_entry,
        mountpoint=mountpoint,
        resolver_path=resolver_path,
        worker_path=worker_path,
        manifest_out=manifest_out,
        observations_out=observations_out,
        source_host='localhost',
        source=f'operator-index-entry:{normalized_entry}',
        evidence_route='index-entry',
        input_kind='index-entry',
        logical_path=normalized_entry.lstrip('/'),
        input_source='operator_index',
        ssh_user=ssh_user,
        log_path=log_path,
        checkpoint_every_host=checkpoint_every_host,
        verbose=verbose,
        brick_host_aliases=brick_host_aliases,
    )


def render_manifest_summary(summary: dict[str, object]) -> str:
    object_types = summary.get("object_types") or {}
    objects = ", ".join(f"{key}={value}" for key, value in sorted(object_types.items()))
    parts = [
        f"Manifest ready: {summary.get('raw_entries', 0)} raw entries",
        f"{summary.get('unique_raw_entries', 0)} unique",
        f"{summary.get('observations', 0)} observations",
    ]
    if objects:
        parts.append(f"objects: {objects}")
    refresh_error = str(summary.get("heal_refresh_error") or "").strip()
    if refresh_error:
        parts.append(f"WARNING: heal refresh failed; captured info is not fresh: {refresh_error}")
    return "; ".join(parts)


def _ssh_reachability_command(host: str, ssh_user: str, connect_timeout: float) -> list[str]:
    timeout_value = str(int(connect_timeout)) if float(connect_timeout).is_integer() else str(connect_timeout)
    return [
        "ssh",
        *ssh_identity_options(),
        "-x",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout_value}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{ssh_user}@{host}",
        "true",
    ]


def build_manager_preflight_report(
    volume: str,
    *,
    ssh_user: str = DEFAULT_SERVICE_USER,
    connect_timeout: float = 10.0,
    require_snapshot: bool = False,
    snapshot_ack: bool = False,
) -> dict[str, object]:
    return build_volume_health_report(
        volume,
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        require_snapshot=require_snapshot,
        snapshot_ack=snapshot_ack,
    )


def build_manager_health_report(
    volume: str,
    *,
    ssh_user: str = DEFAULT_SERVICE_USER,
    connect_timeout: float = 10.0,
    require_snapshot: bool = False,
    snapshot_ack: bool = False,
) -> dict[str, object]:
    return build_manager_preflight_report(
        volume,
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        require_snapshot=require_snapshot,
        snapshot_ack=snapshot_ack,
    )


def render_manager_preflight_summary(report: dict[str, object]) -> str:
    return render_volume_health_summary(report)


def write_manager_preflight_report(path: str | Path, report: dict[str, object]) -> None:
    write_health_report(path, report)


def refresh_manager_health_check(
    status_file: str | Path,
    *,
    current_status: dict[str, object],
    volume: str,
    ssh_user: str = DEFAULT_SERVICE_USER,
    connect_timeout: float = 10.0,
    health_out: str | None = None,
    require_snapshot: bool = False,
    snapshot_ack: bool = False,
) -> tuple[dict[str, object], dict[str, object]]:
    report = build_manager_health_report(
        volume,
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        require_snapshot=require_snapshot,
        snapshot_ack=snapshot_ack,
    )
    target = str(health_out or current_status.get("health_out") or "").strip()
    if target:
        target_path = Path(target).expanduser()
    else:
        target_path = default_health_report_path(volume)
    write_manager_preflight_report(target_path, report)
    layout_path = default_brick_layout_path(volume)
    write_brick_layout_cache(layout_path, report)
    updated_status = update_status(
        status_file,
        volume=volume,
        ssh_user=ssh_user,
        connect_timeout=connect_timeout,
        health_out=str(target_path),
        brick_layout_out=str(layout_path),
        health_check=report,
        health_check_checked_at=report.get("checked_at", ""),
    )
    return updated_status, report
