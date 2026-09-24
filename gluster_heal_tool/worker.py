# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Worker entrypoint helpers."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

from .protocol import CanaryBatchRequest
from .models import ResolutionObservation
from .protocol import ChecksumBatchRequest, ChecksumTarget, ResolveBatchRequest, observations_to_payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gluster-worker")
    sub = parser.add_subparsers(dest="cmd", required=True)

    resolve = sub.add_parser("resolve-batch")
    resolve.add_argument("--input-json")

    checksum = sub.add_parser("checksum-batch")
    checksum.add_argument("--input-json")

    canary = sub.add_parser("canary-batch")
    canary.add_argument("--input-json")
    return parser


def _parse_key_value_output(raw_entry: str, stdout: str) -> ResolutionObservation:
    data: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return ResolutionObservation(
        host=os.uname().nodename.split(".")[0],
        raw_entry=raw_entry,
        gfid=data.get("GFID", ""),
        child_rel=data.get("CHILD_REL", ""),
        gfid_path=data.get("GFID_PATH", ""),
        backend=data.get("BACKEND", ""),
        type=data.get("TYPE", "unknown"),
        gfid_exists=data.get("GFID_EXISTS", "0") == "1",
        backend_gfid_path=data.get("BACKEND_GFID_PATH", ""),
        backend_gfid_path_checked=data.get("BACKEND_GFID_PATH_CHECKED", "0") == "1",
        backend_gfid_path_lexists=data.get("BACKEND_GFID_PATH_LEXISTS", "0") == "1",
        backend_gfid_path_exists=data.get("BACKEND_GFID_PATH_EXISTS", "0") == "1",
        backend_gfid_path_lstat_type=data.get("BACKEND_GFID_PATH_LSTAT_TYPE", ""),
        file_gfid=data.get("FILE_GFID", ""),
        file_gfid_path=data.get("FILE_GFID_PATH", ""),
        relpath=data.get("RELPATH", ""),
        mounted=data.get("MOUNTED", ""),
        depth=int(data.get("DEPTH", "0") or "0"),
    )


def _stat_backend(path: str) -> tuple[bool, int | None, int | None, str]:
    if not path or not os.path.lexists(path):
        return False, None, None, ""
    st = os.lstat(path)
    return True, int(st.st_mtime), int(st.st_size), stat.filemode(st.st_mode)


def _lstat_type(path: str) -> tuple[bool, bool, str, str, int | None, int | None, str]:
    if not path:
        return False, False, "", "", None, None, ""
    lexists = os.path.lexists(path)
    exists = os.path.exists(path)
    if not lexists:
        return False, False, "", "", None, None, ""
    st = os.lstat(path)
    mode = stat.filemode(st.st_mode)
    if stat.S_ISLNK(st.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(st.st_mode):
        kind = "dir"
    elif stat.S_ISREG(st.st_mode):
        kind = "file"
    else:
        kind = "other"
    readlink = os.readlink(path) if stat.S_ISLNK(st.st_mode) else ""
    return lexists, exists, kind, readlink, int(st.st_mtime), int(st.st_size), mode


def _list_backend_children(path: str) -> tuple[list[str], str]:
    if not path or not os.path.isdir(path):
        return [], ""
    try:
        return sorted(os.listdir(path)), ""
    except OSError as exc:
        return [], str(exc)


def _trusted_gfid(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        raw = os.getxattr(path, "trusted.gfid")
    except OSError:
        return ""
    if not raw:
        return ""
    candidates: list[str] = []
    if isinstance(raw, bytes):
        if len(raw) == 16:
            candidates.append(str(uuid.UUID(bytes=raw)))
        text = raw.decode("utf-8", errors="ignore").strip().strip("\x00").strip()
        if text:
            candidates.append(text)
    else:
        text = str(raw).strip().strip("\x00").strip()
        if text:
            candidates.append(text)
    for candidate in candidates:
        try:
            return str(uuid.UUID(candidate))
        except (ValueError, AttributeError, TypeError):
            try:
                return str(uuid.UUID(hex=candidate.replace("-", "")))
            except (ValueError, AttributeError, TypeError):
                continue
    return ""


def _trusted_mdata_hex(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        raw = os.getxattr(path, "trusted.glusterfs.mdata")
    except OSError:
        return ""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        return f"0x{raw.hex()}"
    return f"0x{bytes(str(raw), 'utf-8').hex()}"


def _posix_acl_hex(path: str, name: str) -> tuple[str, str]:
    if not path or not os.path.exists(path):
        return "", ""
    try:
        raw = os.getxattr(path, name)
    except OSError as exc:
        if exc.errno in {errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)}:
            return "", ""
        return "", str(exc)
    if not raw:
        return "", ""
    if isinstance(raw, bytes):
        return f"0x{raw.hex()}", ""
    return f"0x{bytes(str(raw), 'utf-8').hex()}", ""


def _xattr_value_summary(raw: bytes) -> tuple[str, str]:
    value_hex = f"0x{raw.hex()}"
    value_uuid = ""
    if len(raw) == 16:
        try:
            value_uuid = str(uuid.UUID(bytes=raw))
        except (ValueError, AttributeError, TypeError):
            value_uuid = ""
    return value_hex, value_uuid


def _gfid2path_xattr_entry(path: str) -> tuple[str, str]:
    if not path or not os.path.lexists(path):
        return "", ""
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError:
        return "", ""
    for name in sorted(names):
        if not str(name).startswith("trusted.gfid2path"):
            continue
        try:
            raw = os.getxattr(path, str(name), follow_symlinks=False)
        except OSError:
            continue
        if not raw:
            continue
        if isinstance(raw, bytes):
            value = raw.decode("utf-8", errors="ignore")
        else:
            value = str(raw)
        value = value.strip().strip("\x00").strip('"')
        if value:
            return str(name), value
    return "", ""


def _gfid2path_xattr(path: str) -> str:
    _name, value = _gfid2path_xattr_entry(path)
    return value


def _inspect_backend_path(path: str) -> dict[str, object]:
    info: dict[str, object] = {
        "path": path,
        "lexists": os.path.lexists(path),
        "exists": os.path.exists(path),
    }
    if not info["lexists"]:
        return info
    st = os.lstat(path)
    mode = stat.filemode(st.st_mode)
    if stat.S_ISLNK(st.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(st.st_mode):
        kind = "dir"
    elif stat.S_ISREG(st.st_mode):
        kind = "file"
    else:
        kind = "other"
    gfid2path_name, gfid2path_target = _gfid2path_xattr_entry(path)
    acl_access, acl_access_error = _posix_acl_hex(path, "system.posix_acl_access")
    acl_default, acl_default_error = _posix_acl_hex(path, "system.posix_acl_default")
    info.update(
        {
            "kind": kind,
            "mode": mode,
            "mode_bits": int(stat.S_IMODE(st.st_mode)),
            "inode": int(st.st_ino),
            "links": int(st.st_nlink),
            "uid": int(st.st_uid),
            "gid": int(st.st_gid),
            "size": int(st.st_size),
            "mtime": int(st.st_mtime),
            "ctime": int(st.st_ctime),
            "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns,
            "readlink": os.readlink(path) if kind == "symlink" else "",
            "trusted_gfid": _trusted_gfid(path),
            "gfid2path_xattr": gfid2path_name,
            "gfid2path_target": gfid2path_target,
            "acl_access": acl_access,
            "acl_default": acl_default,
            "acl_error": "; ".join(part for part in (acl_access_error, acl_default_error) if part),
        }
    )
    if gfid2path_target:
        reference_target = _normalize_chain_target(path, gfid2path_target)
        info["reference_target"] = reference_target
        info["reference_target_lexists"] = os.path.lexists(reference_target)
        info["reference_target_exists"] = os.path.exists(reference_target)
    return info


def _chmod(path: str, mode_bits: int) -> None:
    if not path:
        raise ValueError("missing path")
    os.chmod(path, mode_bits, follow_symlinks=False)


def _chown(path: str, uid: int, gid: int) -> None:
    if not path:
        raise ValueError("missing path")
    os.chown(path, uid, gid, follow_symlinks=False)


def _setfacl(path: str, acl_text: str) -> None:
    if not path or not acl_text:
        raise ValueError("missing path or ACL text")
    proc = subprocess.run(
        ["setfacl", "--set-file=-", "--", path],
        input=acl_text, text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "setfacl failed")


def _normalize_chain_target(current: str, target: str) -> str:
    if not target:
        return ""
    if os.path.isabs(target):
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(os.path.dirname(current), target))


def _resolve_symlink_chain(path: str, limit: int = 8) -> tuple[str, str, list[str], str]:
    if not path:
        return "", "", [], ""
    chain: list[str] = []
    current = path
    seen: set[str] = set()
    terminal_readlink = ""
    for _ in range(limit):
        if not current or current in seen or not os.path.lexists(current):
            break
        seen.add(current)
        _lexists, exists, kind, readlink, _mtime, _size, _mode = _lstat_type(current)
        target = _gfid2path_xattr(current)
        if kind == "symlink":
            target = target or readlink
        if not target:
            return current, kind, chain, terminal_readlink
        current = _normalize_chain_target(current, target)
        chain.append(current)
        terminal_readlink = target
        if not current or current in seen:
            return current, kind, chain, terminal_readlink
    if not current or not os.path.lexists(current):
        return current, "", chain, terminal_readlink
    _lexists, _exists, kind, readlink, _mtime, _size, _mode = _lstat_type(current)
    return current, kind, chain, readlink if kind == "symlink" else terminal_readlink


def _resolve_reference_target(path: str) -> tuple[str, str, list[str], str]:
    if not path or not os.path.lexists(path):
        return "", "", [], ""
    _lexists, _exists, kind, readlink, _mtime, _size, _mode = _lstat_type(path)
    if kind == "symlink":
        return _resolve_symlink_chain(path)
    target = _gfid2path_xattr(path)
    if not target:
        return "", "", [], ""
    resolved = _normalize_chain_target(path, target)
    chain = [resolved]
    terminal_kind = ""
    terminal_readlink = target
    if os.path.lexists(resolved):
        _terminal_lexists, _terminal_exists, terminal_kind, terminal_readlink, _terminal_mtime, _terminal_size, _terminal_mode = _lstat_type(
            resolved
        )
        if terminal_kind == "symlink":
            terminal_path, terminal_kind, chain, terminal_readlink = _resolve_symlink_chain(resolved)
            if terminal_path:
                resolved = terminal_path
                if not chain:
                    chain = [resolved]
    return resolved, terminal_kind, chain, terminal_readlink


def _stat_mount_path(path: str, mountpoint: str) -> tuple[bool, bool, bool, str, str]:
    if not path or not mountpoint or not os.path.ismount(mountpoint):
        return False, False, False, "", ""
    try:
        lexists, exists, kind, _readlink, _mtime, _size, _mode = _lstat_type(path)
        return True, lexists, exists, kind, ""
    except OSError as exc:
        return True, False, False, "", str(exc)


def _resolve_one(request: ResolveBatchRequest, raw_entry: str) -> ResolutionObservation:
    args = [
        request.resolver_path,
        "-v",
        request.volume,
        "-b",
        request.brick_path,
        "-m",
        request.mountpoint,
        "-e",
        raw_entry,
        "-k",
    ]
    if request.verbose:
        args.append("-V")
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    obs = _parse_key_value_output(raw_entry, proc.stdout)
    if proc.returncode != 0:
        obs.error = proc.stderr.strip() or f"resolver exit {proc.returncode}"
        return obs
    if obs.backend:
        lexists, exists, kind, readlink, mtime, size, mode = _lstat_type(obs.backend)
        obs.backend_lexists = lexists
        obs.backend_exists = exists
        obs.backend_mtime = mtime
        obs.backend_size = size
        obs.backend_mode = mode
        obs.backend_lstat_type = kind
        obs.backend_is_symlink = kind == "symlink"
        obs.backend_readlink = readlink
        backend_info = _inspect_backend_path(obs.backend)
        obs.backend_mtime_ns = backend_info.get("mtime_ns")
        obs.backend_ctime_ns = backend_info.get("ctime_ns")
        obs.backend_inode = backend_info.get("inode")
        obs.backend_mode_bits = backend_info.get("mode_bits")
        obs.backend_uid = backend_info.get("uid")
        obs.backend_gid = backend_info.get("gid")
        obs.backend_acl_access_text = str(backend_info.get("acl_access") or "")
        obs.backend_acl_default_text = str(backend_info.get("acl_default") or "")
        obs.backend_acl_error = str(backend_info.get("acl_error") or "")
        if obs.backend_is_symlink:
            terminal_path, terminal_kind, chain, terminal_readlink = _resolve_symlink_chain(obs.backend)
            obs.backend_readlink_chain = chain
            obs.backend_terminal_path = terminal_path
            obs.backend_terminal_lstat_type = terminal_kind
            obs.backend_terminal_exists = bool(terminal_path and os.path.lexists(terminal_path))
            obs.backend_terminal_is_symlink = terminal_kind == "symlink"
            obs.backend_terminal_readlink = terminal_readlink
            obs.backend_terminal_trusted_gfid = _trusted_gfid(terminal_path)
        else:
            terminal_path, terminal_kind, chain, terminal_readlink = _resolve_reference_target(obs.backend)
            if terminal_path:
                obs.backend_readlink_chain = chain
                obs.backend_terminal_path = terminal_path
                obs.backend_terminal_exists = bool(terminal_path and os.path.lexists(terminal_path))
                obs.backend_terminal_lstat_type = terminal_kind
                obs.backend_terminal_is_symlink = terminal_kind == "symlink"
                obs.backend_terminal_readlink = terminal_readlink
                obs.backend_terminal_trusted_gfid = _trusted_gfid(terminal_path)
        obs.backend_trusted_gfid = _trusted_gfid(obs.backend)
        obs.backend_mdata_hex = _trusted_mdata_hex(obs.backend)
        if obs.backend_exists and obs.backend_lstat_type == "dir":
            obs.backend_child_names, obs.backend_child_scan_error = _list_backend_children(obs.backend)
    if obs.gfid_path:
        lexists, exists, kind, readlink, _mtime, _size, _mode = _lstat_type(obs.gfid_path)
        obs.gfid_path_lexists = lexists
        obs.gfid_path_exists = exists
        obs.gfid_path_lstat_type = kind
        obs.gfid_path_is_symlink = kind == "symlink"
        obs.gfid_path_readlink = readlink
        if obs.gfid_path_is_symlink:
            terminal_path, terminal_kind, chain, terminal_readlink = _resolve_symlink_chain(obs.gfid_path)
            obs.gfid_path_readlink_chain = chain
            obs.gfid_path_terminal_exists = bool(terminal_path and os.path.lexists(terminal_path))
            obs.gfid_path_terminal_path = terminal_path
            obs.gfid_path_terminal_lstat_type = terminal_kind
            obs.gfid_path_terminal_is_symlink = terminal_kind == "symlink"
            obs.gfid_path_terminal_readlink = terminal_readlink
            obs.gfid_path_terminal_trusted_gfid = _trusted_gfid(terminal_path)
        else:
            terminal_path, terminal_kind, chain, terminal_readlink = _resolve_reference_target(obs.gfid_path)
            if terminal_path:
                obs.gfid_path_readlink_chain = chain
                obs.gfid_path_terminal_path = terminal_path
                obs.gfid_path_terminal_exists = bool(terminal_path and os.path.lexists(terminal_path))
                obs.gfid_path_terminal_lstat_type = terminal_kind
                obs.gfid_path_terminal_is_symlink = terminal_kind == "symlink"
                obs.gfid_path_terminal_readlink = terminal_readlink
                obs.gfid_path_terminal_trusted_gfid = _trusted_gfid(terminal_path)
    if obs.mounted and request.probe_mount:
        checked, lexists, exists, kind, error = _stat_mount_path(obs.mounted, request.mountpoint)
        obs.mounted_checked = checked
        obs.mounted_lexists = lexists
        obs.mounted_exists = exists
        obs.mounted_lstat_type = kind
        obs.mounted_error = error
    elif obs.mounted:
        obs.mounted_checked = False
    return obs


def _sha256_file(path: str) -> tuple[str, str]:
    if not path:
        return "", "empty path"
    if not os.path.exists(path):
        return "", "missing"
    if not os.path.isfile(path):
        return "", "not a regular file"
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), ""


def _normalize_hex(value: str) -> str:
    cleaned = value.strip().lower()
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    cleaned = cleaned.replace("-", "")
    return cleaned


def _gfid_uuid_from_raw(raw: bytes) -> str:
    if len(raw) != 16:
        raise ValueError(f"expected 16-byte GFID, got {len(raw)} bytes")
    return str(uuid.UUID(bytes=raw))


def _gfid_hex_from_raw(raw: bytes) -> str:
    return raw.hex()


def _require_under_root(root: str, path: str) -> None:
    if not root:
        return
    root_path = os.path.abspath(os.path.normpath(root))
    candidate_path = os.path.abspath(os.path.normpath(path))
    try:
        common = os.path.commonpath([root_path, candidate_path])
    except ValueError as exc:
        raise ValueError(f"path {path!r} is not under backend root {root!r}") from exc
    if common != root_path:
        raise ValueError(f"path {path!r} is not under backend root {root!r}")


def _mkdir_p(path: str) -> None:
    if not path:
        raise ValueError("missing path")
    Path(path).mkdir(parents=True, exist_ok=True)


def _touch(path: str) -> None:
    if not path:
        raise ValueError("missing path")
    _mkdir_p(str(Path(path).parent))
    Path(path).touch(exist_ok=True)


def _write_text(path: str, text: str) -> None:
    if not path:
        raise ValueError("missing path")
    _mkdir_p(str(Path(path).parent))
    Path(path).write_text(text, encoding="utf-8")


def _remove_path(path: str, recursive: bool, force: bool) -> None:
    if not path:
        raise ValueError("missing path")
    candidate = Path(path)
    if not candidate.exists() and not candidate.is_symlink():
        if force:
            return
        raise FileNotFoundError(path)
    if recursive and candidate.is_dir() and not candidate.is_symlink():
        shutil.rmtree(candidate)
        return
    candidate.unlink(missing_ok=force)


def _set_xattr(path: str, name: str, value_hex: str) -> None:
    if not path:
        raise ValueError("missing path")
    if not name:
        raise ValueError("missing xattr name")
    normalized = _normalize_hex(value_hex)
    if not normalized:
        raise ValueError("missing xattr value")
    value = bytes.fromhex(normalized)
    os.setxattr(path, name, value, follow_symlinks=False)


def _remove_xattr(path: str, name: str) -> None:
    if not path:
        raise ValueError("missing path")
    if not name:
        raise ValueError("missing xattr name")
    try:
        os.removexattr(path, name, follow_symlinks=False)
    except OSError as exc:
        if exc.errno != errno.ENODATA:
            raise


def _get_xattr(path: str, name: str) -> bytes:
    if not path:
        raise ValueError("missing path")
    if not name:
        raise ValueError("missing xattr name")
    return os.getxattr(path, name, follow_symlinks=False)


def _inspect_afr_state(path: str) -> dict[str, object]:
    if not path:
        raise ValueError("missing path")
    if not os.path.lexists(path):
        return {
            "path": path,
            "afr_xattrs": [],
            "afr_xattr_names": [],
        }
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc
    afr_xattrs: list[dict[str, object]] = []
    for name in sorted(str(item) for item in names if str(item).startswith("trusted.afr.")):
        try:
            raw = os.getxattr(path, name, follow_symlinks=False)
        except OSError as exc:
            afr_xattrs.append({"name": name, "error": str(exc)})
            continue
        raw_bytes = raw if isinstance(raw, bytes) else bytes(str(raw), "utf-8")
        value_hex, _value_uuid = _xattr_value_summary(raw_bytes)
        afr_xattrs.append(
            {
                "name": name,
                "value_hex": value_hex,
                "value_bytes": len(raw_bytes),
            }
        )
    return {
        "path": path,
        "afr_xattrs": afr_xattrs,
        "afr_xattr_names": [str(item.get("name") or "") for item in afr_xattrs if str(item.get("name") or "")],
    }


def _require_canary_object_path(request: CanaryBatchRequest, path: str) -> None:
    _require_under_root(request.backend_root, path)
    scenario = str(request.scenario or "").strip()
    if not scenario:
        return
    root_path = os.path.abspath(os.path.normpath(request.backend_root))
    candidate_path = os.path.abspath(os.path.normpath(path))
    relative = os.path.relpath(candidate_path, root_path)
    if relative == os.pardir or relative.startswith(f"{os.pardir}{os.sep}"):
        raise ValueError(f"path {path!r} is not under canary scenario {scenario!r}")
    first_component = relative.split(os.sep, 1)[0]
    if first_component != scenario:
        raise ValueError(f"path {path!r} is not under canary scenario {scenario!r}")


def _set_afr_metadata_pending(path: str, pending_names: list[str], value_hex: str) -> dict[str, object]:
    if not path:
        raise ValueError("missing path")
    names = [str(name) for name in pending_names if str(name)]
    if not names:
        raise ValueError("missing afr pending xattr names")
    normalized = _normalize_hex(value_hex)
    if not normalized:
        raise ValueError("missing afr pending xattr value")
    value_bytes = bytes.fromhex(normalized)
    afr_xattrs: list[dict[str, object]] = []
    for name in names:
        if not name.startswith("trusted.afr."):
            raise ValueError("canary AFR pending xattrs must start with trusted.afr.")
        _set_xattr(path, name, normalized)
        afr_xattrs.append(
            {
                "name": name,
                "value_hex": f"0x{normalized}",
                "value_bytes": len(value_bytes),
            }
        )
    return {
        "path": path,
        "afr_xattrs": afr_xattrs,
        "afr_xattr_names": names,
        "value_hex": f"0x{normalized}",
    }


def _index_entry_candidates(
    *,
    gfid_uuid: str = "",
    gfid_hex: str = "",
    raw: bytes | None = None,
) -> list[str]:
    names: list[str] = []
    if raw is not None:
        names.append(_gfid_uuid_from_raw(raw))
        names.append(_gfid_hex_from_raw(raw))
    if gfid_uuid:
        canonical = str(uuid.UUID(gfid_uuid))
        names.append(canonical)
        names.append(canonical.replace("-", ""))
    if gfid_hex:
        normalized = _normalize_hex(gfid_hex)
        if normalized:
            names.append(str(uuid.UUID(hex=normalized)))
            names.append(normalized)
    ordered: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _index_entry_from_target(target_path: str, index_root: str) -> tuple[str, str]:
    raw_gfid = _get_xattr(target_path, "trusted.gfid")
    canonical = _gfid_uuid_from_raw(raw_gfid)
    entry_path = os.path.join(index_root, canonical)
    _mkdir_p(index_root)
    _remove_path(entry_path, recursive=False, force=True)
    _touch(entry_path)
    return canonical, entry_path


def _entry_changes_index_from_parent(parent_path: str, index_root: str, child_name: str) -> tuple[str, str]:
    if not child_name or "/" in child_name:
        raise ValueError("entry-changes child name must be a single path component")
    raw_gfid = _get_xattr(parent_path, "trusted.gfid")
    canonical = _gfid_uuid_from_raw(raw_gfid)
    parent_index = os.path.join(index_root, canonical)
    entry_path = os.path.join(parent_index, child_name)
    _mkdir_p(parent_index)
    _remove_path(entry_path, recursive=False, force=True)
    _touch(entry_path)
    return canonical, entry_path


def _file_gfid_link_path(backend_root: str, canonical_gfid: str) -> str:
    compact = canonical_gfid.replace("-", "")
    return os.path.join(backend_root, ".glusterfs", compact[:2], compact[2:4], canonical_gfid)


def _link_file_gfid_from_target(target_path: str, backend_root: str) -> tuple[str, str]:
    if not os.path.isfile(target_path) or os.path.islink(target_path):
        raise ValueError("file GFID links can only be created for regular files")
    raw_gfid = _get_xattr(target_path, "trusted.gfid")
    canonical = _gfid_uuid_from_raw(raw_gfid)
    gfid_path = _file_gfid_link_path(backend_root, canonical)
    _mkdir_p(str(Path(gfid_path).parent))
    if os.path.lexists(gfid_path):
        try:
            if os.path.samefile(target_path, gfid_path):
                return canonical, gfid_path
        except OSError:
            pass
        _remove_path(gfid_path, recursive=False, force=True)
    os.link(target_path, gfid_path)
    return canonical, gfid_path


def _directory_gfid_link_path(backend_root: str, canonical_gfid: str) -> str:
    compact = canonical_gfid.replace("-", "")
    return os.path.join(backend_root, ".glusterfs", compact[:2], compact[2:4], canonical_gfid)


def _link_directory_gfid_from_target(target_path: str, backend_root: str) -> tuple[str, str]:
    if not os.path.isdir(target_path) or os.path.islink(target_path):
        raise ValueError("directory GFID links can only be created for regular directories")
    raw_gfid = _get_xattr(target_path, "trusted.gfid")
    canonical = _gfid_uuid_from_raw(raw_gfid)
    gfid_path = _directory_gfid_link_path(backend_root, canonical)
    _mkdir_p(str(Path(gfid_path).parent))
    if os.path.lexists(gfid_path):
        try:
            if os.path.islink(gfid_path) and os.readlink(gfid_path) == target_path:
                return canonical, gfid_path
        except OSError:
            pass
        _remove_path(gfid_path, recursive=False, force=True)
    os.symlink(target_path, gfid_path)
    return canonical, gfid_path


def _link_xattrop_gfid_from_target(target_path: str, backend_root: str) -> tuple[str, str]:
    index_root = os.path.join(backend_root, ".glusterfs", "indices", "xattrop")
    return _index_entry_from_target(target_path, index_root)


def _remove_index_entries(
    *,
    target_path: str = "",
    index_root: str = "",
    gfid_uuid: str = "",
    gfid_hex: str = "",
    entry_name_candidates: list[str] | None = None,
) -> list[str]:
    candidates: list[str] = []
    if entry_name_candidates:
        candidates.extend(entry_name_candidates)
    if target_path:
        try:
            raw_gfid = _get_xattr(target_path, "trusted.gfid")
        except OSError:
            raw_gfid = b""
        if raw_gfid:
            candidates.extend(_index_entry_candidates(raw=raw_gfid))
    candidates.extend(_index_entry_candidates(gfid_uuid=gfid_uuid, gfid_hex=gfid_hex))
    ordered: list[str] = []
    seen: set[str] = set()
    for name in candidates:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    removed: list[str] = []
    for name in ordered:
        entry_path = os.path.join(index_root, name)
        if not os.path.exists(entry_path) and not os.path.islink(entry_path):
            continue
        _remove_path(entry_path, recursive=False, force=True)
        removed.append(entry_path)
    return removed


def _remove_entry_changes_entries(
    *,
    parent_path: str = "",
    index_root: str = "",
    child_name: str = "",
    parent_gfid_uuid: str = "",
    parent_gfid_hex: str = "",
) -> list[str]:
    if not child_name or "/" in child_name:
        raise ValueError("entry-changes child name must be a single path component")
    candidates: list[str] = []
    if parent_path:
        try:
            raw_gfid = _get_xattr(parent_path, "trusted.gfid")
        except OSError:
            raw_gfid = b""
        if raw_gfid:
            candidates.extend(_index_entry_candidates(raw=raw_gfid))
    candidates.extend(_index_entry_candidates(gfid_uuid=parent_gfid_uuid, gfid_hex=parent_gfid_hex))

    removed: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        parent_index = os.path.join(index_root, candidate)
        entry_path = os.path.join(parent_index, child_name)
        if os.path.exists(entry_path) or os.path.islink(entry_path):
            _remove_path(entry_path, recursive=False, force=True)
            removed.append(entry_path)
        try:
            os.rmdir(parent_index)
            removed.append(parent_index)
        except OSError:
            pass
    return removed


def _run_canary_op(request: CanaryBatchRequest, op: dict[str, object]) -> dict[str, object]:
    kind = str(op.get("op") or "").strip()
    result: dict[str, object] = {
        "op": kind,
        "ok": False,
        "path": str(op.get("path") or ""),
    }
    try:
        if kind == "mkdir":
            path = str(op.get("path") or "")
            _require_under_root(request.backend_root, path)
            _mkdir_p(path)
            result["ok"] = True
            result["path"] = path
            return result
        if kind == "touch":
            path = str(op.get("path") or "")
            _require_under_root(request.backend_root, path)
            _touch(path)
            result["ok"] = True
            result["path"] = path
            return result
        if kind == "write_text":
            path = str(op.get("path") or "")
            text = str(op.get("text") or "")
            _require_under_root(request.backend_root, path)
            _write_text(path, text)
            result["ok"] = True
            result["path"] = path
            result["bytes"] = len(text.encode("utf-8"))
            return result
        if kind == "chmod":
            path = str(op.get("path") or "")
            mode_bits_raw = str(op.get("mode_bits") or op.get("mode") or "")
            _require_under_root(request.backend_root, path)
            if not mode_bits_raw:
                raise ValueError("canary chmod requires mode_bits or mode")
            mode_bits = int(mode_bits_raw, 8) if isinstance(mode_bits_raw, str) else int(mode_bits_raw)
            _chmod(path, mode_bits)
            result["ok"] = True
            result["path"] = path
            result["mode_bits"] = mode_bits
            return result
        if kind == "chown":
            path = str(op.get("path") or "")
            uid = int(op.get("uid") if op.get("uid") is not None else -1)
            gid = int(op.get("gid") if op.get("gid") is not None else -1)
            _require_under_root(request.backend_root, path)
            if uid < 0 and gid < 0:
                raise ValueError("canary chown requires uid or gid")
            _chown(path, uid, gid)
            result["ok"] = True
            result["path"] = path
            result["uid"] = uid
            result["gid"] = gid
            return result
        if kind == "setfacl":
            path = str(op.get("path") or "")
            acl_text = str(op.get("acl_text") or "")
            _require_under_root(request.backend_root, path)
            _setfacl(path, acl_text)
            result["ok"] = True
            result["path"] = path
            return result
        if kind == "rm":
            path = str(op.get("path") or "")
            recursive = bool(op.get("recursive", False))
            force = bool(op.get("force", True))
            _require_under_root(request.backend_root, path)
            _remove_path(path, recursive=recursive, force=force)
            result["ok"] = True
            result["path"] = path
            return result
        if kind == "setxattr":
            path = str(op.get("path") or "")
            name = str(op.get("name") or "")
            value_hex = str(op.get("value_hex") or "")
            _require_under_root(request.backend_root, path)
            if not (
                name == "trusted.gfid"
                or name == "trusted.glusterfs.mdata"
                or name.startswith("trusted.afr.")
                or name.startswith("trusted.gfid2path")
            ):
                raise ValueError(
                    "canary setxattr only allows trusted.gfid, trusted.glusterfs.mdata, trusted.afr.*, or trusted.gfid2path*"
                )
            _set_xattr(path, name, value_hex)
            result["ok"] = True
            result["path"] = path
            result["name"] = name
            return result
        if kind == "removexattr":
            path = str(op.get("path") or "")
            name = str(op.get("name") or "")
            _require_under_root(request.backend_root, path)
            if not (
                name == "trusted.gfid"
                or name == "trusted.glusterfs.mdata"
                or name.startswith("trusted.afr.")
                or name.startswith("trusted.gfid2path")
            ):
                raise ValueError(
                    "canary removexattr only allows trusted.gfid, trusted.glusterfs.mdata, trusted.afr.*, or trusted.gfid2path*"
                )
            _remove_xattr(path, name)
            result["ok"] = True
            result["path"] = path
            result["name"] = name
            return result
        if kind == "getxattr":
            path = str(op.get("path") or "")
            name = str(op.get("name") or "")
            _require_under_root(request.backend_root, path)
            if name not in {"trusted.gfid", "trusted.glusterfs.mdata"}:
                raise ValueError("canary getxattr only allows trusted.gfid or trusted.glusterfs.mdata")
            raw = _get_xattr(path, name)
            result["ok"] = True
            result["path"] = path
            result["name"] = name
            value_hex, value_uuid = _xattr_value_summary(raw)
            result["value_hex"] = value_hex
            if value_uuid:
                result["value_uuid"] = value_uuid
            return result
        if kind == "inspect_afr_state":
            path = str(op.get("path") or "")
            _require_canary_object_path(request, path)
            afr_state = _inspect_afr_state(path)
            result["ok"] = True
            result["path"] = path
            result["afr_xattrs"] = afr_state["afr_xattrs"]
            result["afr_xattr_names"] = afr_state["afr_xattr_names"]
            return result
        if kind == "set_afr_metadata_pending":
            path = str(op.get("path") or "")
            pending_names = [str(item) for item in op.get("pending_names", []) if str(item)]
            value_hex = str(op.get("value_hex") or "")
            _require_canary_object_path(request, path)
            afr_state = _set_afr_metadata_pending(path, pending_names, value_hex)
            result["ok"] = True
            result["path"] = path
            result["afr_xattrs"] = afr_state["afr_xattrs"]
            result["afr_xattr_names"] = afr_state["afr_xattr_names"]
            result["value_hex"] = afr_state["value_hex"]
            return result
        if kind == "inspect_path":
            path = str(op.get("path") or "")
            label = str(op.get("label") or "")
            _require_under_root(request.backend_root, path)
            result.update(_inspect_backend_path(path))
            result["ok"] = True
            if label:
                result["label"] = label
            return result
        if kind == "touch_index_from_target":
            target_path = str(op.get("target_path") or op.get("path") or "")
            index_root = str(op.get("index_root") or "")
            _require_under_root(request.backend_root, target_path)
            _require_under_root(request.backend_root, index_root)
            canonical, entry_path = _index_entry_from_target(target_path, index_root)
            result["ok"] = True
            result["path"] = target_path
            result["index_root"] = index_root
            result["index_entry"] = entry_path
            result["value_uuid"] = canonical
            result["value_hex"] = canonical.replace("-", "")
            return result
        if kind == "link_xattrop_gfid":
            target_path = str(op.get("target_path") or op.get("path") or "")
            _require_canary_object_path(request, target_path)
            canonical, xattrop_path = _link_xattrop_gfid_from_target(target_path, request.backend_root)
            _require_under_root(request.backend_root, xattrop_path)
            result["ok"] = True
            result["path"] = target_path
            result["xattrop_entry"] = xattrop_path
            result["value_uuid"] = canonical
            result["value_hex"] = canonical.replace("-", "")
            return result
        if kind == "link_file_gfid_from_target":
            target_path = str(op.get("target_path") or op.get("path") or "")
            _require_under_root(request.backend_root, target_path)
            canonical, gfid_path = _link_file_gfid_from_target(target_path, request.backend_root)
            _require_under_root(request.backend_root, gfid_path)
            result["ok"] = True
            result["path"] = target_path
            result["file_gfid_path"] = gfid_path
            result["value_uuid"] = canonical
            result["value_hex"] = canonical.replace("-", "")
            return result
        if kind == "link_directory_gfid_from_target":
            target_path = str(op.get("target_path") or op.get("path") or "")
            _require_under_root(request.backend_root, target_path)
            canonical, gfid_path = _link_directory_gfid_from_target(target_path, request.backend_root)
            _require_under_root(request.backend_root, gfid_path)
            result["ok"] = True
            result["path"] = target_path
            result["directory_gfid_path"] = gfid_path
            result["value_uuid"] = canonical
            result["value_hex"] = canonical.replace("-", "")
            return result
        if kind == "touch_entry_changes_index_from_parent":
            parent_path = str(op.get("parent_path") or op.get("target_path") or op.get("path") or "")
            index_root = str(op.get("index_root") or "")
            child_name = str(op.get("child_name") or "")
            _require_under_root(request.backend_root, parent_path)
            _require_under_root(request.backend_root, index_root)
            canonical, entry_path = _entry_changes_index_from_parent(parent_path, index_root, child_name)
            result["ok"] = True
            result["path"] = parent_path
            result["index_root"] = index_root
            result["child_name"] = child_name
            result["index_entry"] = entry_path
            result["parent_gfid_uuid"] = canonical
            result["parent_gfid_hex"] = canonical.replace("-", "")
            return result
        if kind == "remove_afr_fixture":
            target_path = str(op.get("target_path") or op.get("path") or "")
            index_root = str(op.get("index_root") or os.path.join(request.backend_root, ".glusterfs", "indices", "xattrop"))
            pending_names = [str(item) for item in op.get("pending_names", []) if str(item)]
            _require_canary_object_path(request, target_path)
            _require_under_root(request.backend_root, index_root)
            afr_state = _inspect_afr_state(target_path)
            names_to_remove = pending_names or [
                str(name)
                for name in afr_state.get("afr_xattr_names") or []
                if str(name).startswith("trusted.afr.")
            ]
            removed_xattrs: list[str] = []
            for name in names_to_remove:
                if not name.startswith("trusted.afr."):
                    raise ValueError("canary AFR fixture removal only allows trusted.afr.* names")
                _remove_xattr(target_path, name)
                removed_xattrs.append(name)
            removed_index_entries = _remove_index_entries(target_path=target_path, index_root=index_root)
            result["ok"] = True
            result["path"] = target_path
            result["index_root"] = index_root
            result["removed_xattrs"] = removed_xattrs
            result["removed_index_entries"] = removed_index_entries
            return result
        if kind == "rm_index_from_target":
            target_path = str(op.get("target_path") or op.get("path") or "")
            index_root = str(op.get("index_root") or "")
            gfid_uuid = str(op.get("gfid_uuid") or "")
            gfid_hex = str(op.get("gfid_hex") or "")
            entry_name_candidates = [str(item) for item in op.get("entry_name_candidates", []) if str(item)]
            _require_under_root(request.backend_root, index_root)
            if target_path:
                _require_under_root(request.backend_root, target_path)
            removed = _remove_index_entries(
                target_path=target_path,
                index_root=index_root,
                gfid_uuid=gfid_uuid,
                gfid_hex=gfid_hex,
                entry_name_candidates=entry_name_candidates,
            )
            result["ok"] = True
            result["path"] = target_path
            result["index_root"] = index_root
            result["removed"] = removed
            return result
        if kind == "rm_entry_changes_index_from_parent":
            parent_path = str(op.get("parent_path") or op.get("target_path") or op.get("path") or "")
            index_root = str(op.get("index_root") or "")
            child_name = str(op.get("child_name") or "")
            parent_gfid_uuid = str(op.get("parent_gfid_uuid") or op.get("gfid_uuid") or "")
            parent_gfid_hex = str(op.get("parent_gfid_hex") or op.get("gfid_hex") or "")
            _require_under_root(request.backend_root, index_root)
            if parent_path:
                _require_under_root(request.backend_root, parent_path)
            removed = _remove_entry_changes_entries(
                parent_path=parent_path,
                index_root=index_root,
                child_name=child_name,
                parent_gfid_uuid=parent_gfid_uuid,
                parent_gfid_hex=parent_gfid_hex,
            )
            result["ok"] = True
            result["path"] = parent_path
            result["index_root"] = index_root
            result["child_name"] = child_name
            result["removed"] = removed
            return result
        raise ValueError(f"unsupported canary op: {kind}")
    except Exception as exc:  # pragma: no cover - exercised in live runs
        result["error"] = str(exc)
        return result


def run_canary_batch(input_json: str | None) -> int:
    payload = json.loads(sys.stdin.read()) if not input_json else json.loads(open(input_json).read())
    request = CanaryBatchRequest(**payload)
    results = [_run_canary_op(request, op) for op in request.ops]
    json.dump(
        {
            "host": os.uname().nodename.split(".")[0],
            "volume": request.volume,
            "scenario": request.scenario,
            "backend_root": request.backend_root,
            "results": results,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


def run_resolve_batch(input_json: str | None) -> int:
    payload = json.loads(sys.stdin.read()) if not input_json else json.loads(open(input_json).read())
    request = ResolveBatchRequest(**payload)
    observations = [_resolve_one(request, entry) for entry in request.entries]
    json.dump(observations_to_payload(observations), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def run_checksum_batch(input_json: str | None) -> int:
    payload = json.loads(sys.stdin.read()) if not input_json else json.loads(open(input_json).read())
    request = ChecksumBatchRequest(
        items=[ChecksumTarget(**item) for item in payload.get("items", [])]
    )
    results: list[dict[str, str]] = []
    for item in request.items:
        checksum, error = _sha256_file(item.backend_path)
        results.append(
            {
                "host": os.uname().nodename.split(".")[0],
                "logical_path": item.logical_path,
                "backend_path": item.backend_path,
                "sha256": checksum,
                "error": error,
            }
        )
    json.dump({"checksums": results}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "resolve-batch":
        return run_resolve_batch(args.input_json)
    if args.cmd == "checksum-batch":
        return run_checksum_batch(args.input_json)
    if args.cmd == "canary-batch":
        return run_canary_batch(args.input_json)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
